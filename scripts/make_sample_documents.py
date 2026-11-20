"""Write sample applications and documents to ./samples for manual testing via the API / console."""
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from onboarding.samples import documents_for, make_application, pan_card_image  # noqa: E402

out = ROOT / "samples"
out.mkdir(exist_ok=True)
rng = random.Random(2026)

form = make_application(name="Sharma Electronics", rng=rng)
(out / "application_clean.json").write_text(json.dumps({"form": form, "documents": documents_for(form)}, indent=2))

bad = make_application(name="Lakshmi Textiles", gst_entity="9", rng=rng)
(out / "application_gst_cancelled.json").write_text(json.dumps({"form": bad, "documents": documents_for(bad)}, indent=2))

(out / "pan_card_clear.png").write_bytes(pan_card_image(form["pan"], form["legal_name"])["content"])
(out / "pan_card_blurred.png").write_bytes(pan_card_image(form["pan"], form["legal_name"], blur=6)["content"])
for d in documents_for(form):
    (out / d["filename"]).write_text(d["text"])
print(f"wrote samples to {out}")
