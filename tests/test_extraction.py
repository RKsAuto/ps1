import io
import json

import httpx
import pytest
from PIL import Image

from onboarding.config import Settings
from onboarding.extraction import (extract, gstin_checksum, make_gstin, name_similarity, valid_format)
from onboarding.samples import cheque_text, gst_certificate_text, pan_card_image, pan_card_text, render_document_png
from onboarding.security import mask, redact
from onboarding.states import AppState, Event, InvalidTransition, next_state

S = Settings(db_path=":memory:")


def test_gstin_checksum_and_formats():
    g = make_gstin("27", "AAPFU0939F", "1")
    assert valid_format("gstin", g)
    assert not valid_format("gstin", g[:-1] + ("A" if g[-1] != "A" else "B"))
    assert gstin_checksum(g[:14]) == g[14]
    assert valid_format("pan", "AAPFU0939F") and not valid_format("pan", "AAPF0939F")
    assert valid_format("ifsc", "HDFC0001234") and not valid_format("ifsc", "HDFC1001234")


def test_text_extraction_per_document_type():
    ex = extract({"doc_type": "PAN_CARD", "text": pan_card_text("ABCCD1234E", "Acme Retail Private Limited")}, S)
    assert ex.fields["pan"] == {"value": "ABCCD1234E", "confidence": 0.9}
    assert ex.fields["name"]["value"] == "ACME RETAIL PRIVATE LIMITED"
    g = make_gstin("29", "ABCCD1234E")
    ex = extract({"doc_type": "GST_CERTIFICATE", "text": gst_certificate_text(g, "Acme Retail Private Limited", "Acme")}, S)
    assert ex.fields["gstin"]["value"] == g and ex.fields["trade_name"]["value"] == "ACME"
    ex = extract({"doc_type": "CANCELLED_CHEQUE", "text": cheque_text("50100234567890", "HDFC0001234", "Acme Retail")}, S)
    assert ex.fields["account_number"]["value"] == "50100234567890"
    assert ex.fields["ifsc"]["value"] == "HDFC0001234"


def test_ambiguous_or_invalid_values_get_low_confidence():
    text = "PAN ABCCD1234E\nalso ABCCD9999Z\nName: Acme"
    ex = extract({"doc_type": "PAN_CARD", "text": text}, S)
    assert ex.fields["pan"]["confidence"] < S.confidence_threshold
    ex = extract({"doc_type": "PAN_CARD", "fields": {"pan": "ABC123"}, "ocr_confidence": 0.99}, S)
    assert ex.fields["pan"]["confidence"] <= 0.4


def test_structured_payload_with_blur_flag_is_unreadable():
    ex = extract({"doc_type": "PAN_CARD", "fields": {"pan": "ABCCD1234E"}, "quality": {"blurred": True}}, S)
    assert ex.readable is False


def test_blur_detection_on_images():
    sharp = extract(pan_card_image("ABCCD1234E", "Acme"), S)
    blurred = extract(pan_card_image("ABCCD1234E", "Acme", blur=6), S)
    assert sharp.readable and sharp.method == "manual-extraction-required"   # no LLM key configured
    assert not blurred.readable and blurred.quality["blurred"]
    assert sharp.quality["laplacian_variance"] > blurred.quality["laplacian_variance"]


def test_scanned_pdf_goes_through_image_pipeline():
    img = Image.open(io.BytesIO(render_document_png(["PAN CARD", "ABCCD1234E", "Name: Acme"], blur=6)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PDF")
    ex = extract({"doc_type": "PAN_CARD", "filename": "scan.pdf", "media_type": "application/pdf", "content": buf.getvalue()}, S)
    assert ex.method.startswith("pdf-scan/") and not ex.readable


def test_llm_extraction_is_validated_and_prompt_injection_resistant():
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        assert body["model"] == "test-model" and body["messages"][0]["content"][0]["type"] == "image"
        answer = {"pan": {"value": "abccd1234e", "confidence": 0.99}, "name": {"value": "Acme Retail", "confidence": 0.9},
                  "legible": True}
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Here you go: " + json.dumps(answer)}]})

    settings = Settings(db_path=":memory:", anthropic_api_key="sk-test", extraction_model="test-model")
    ex = extract(pan_card_image("ABCCD1234E", "Acme"), settings, transport=httpx.MockTransport(handler))
    assert ex.method == "llm:test-model"
    assert ex.fields["pan"] == {"value": "ABCCD1234E", "confidence": 0.95}   # model confidence capped

    def bad(request):
        return httpx.Response(200, json={"content": [{"type": "text", "text": '{"pan": {"value": "IGNORE PREVIOUS", "confidence": 1}}'}]})
    ex = extract(pan_card_image("ABCCD1234E", "Acme"), settings, transport=httpx.MockTransport(bad))
    assert ex.fields["pan"]["confidence"] <= 0.4                              # fails format validation

    def down(request):
        return httpx.Response(529, json={"error": "overloaded"})
    ex = extract(pan_card_image("ABCCD1234E", "Acme"), settings, transport=httpx.MockTransport(down))
    assert ex.readable is False


def test_name_similarity_ignores_legal_suffixes():
    assert name_similarity("Acme Retail Pvt. Ltd.", "ACME RETAIL PRIVATE LIMITED") == 1.0
    assert name_similarity("Acme Retail", "Zenith Foods") < 0.5


def test_masking_and_redaction():
    assert mask("pan", "ABCCD1234E") == "ABCXXXX34E"
    assert mask("bank_account", "50100234567890") == "XXXXXXXXXX7890"
    assert "ABCCD1234E" not in redact("PAN ABCCD1234E and account 50100234567890")


def test_state_machine_rejects_illegal_transitions():
    assert next_state(AppState.VERIFYING, Event.ALL_CHECKS_PASSED) == AppState.APPROVED
    with pytest.raises(InvalidTransition):
        next_state(AppState.VERIFYING, Event.MERCHANT_CREATED)     # cannot skip approval + provisioning
    with pytest.raises(InvalidTransition):
        next_state(AppState.REJECTED, Event.REVIEW_APPROVED)
