import hashlib
import importlib.util
from io import BytesIO
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


config = ModuleType("config")
config.load_config = lambda: SimpleNamespace(
    smtp=SimpleNamespace(from_header="Newsletter <sender@example.com>"),
    web=SimpleNamespace(
        token_secret="test-secret",
        public_base_url="https://example.com",
        unsubscribe_path="/unsubscribe",
    ),
)
spec = importlib.util.spec_from_file_location(
    "relay_images", Path(__file__).resolve().parents[1] / "src" / "replay-daemon.py"
)
relay = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"config": config}):
    spec.loader.exec_module(relay)


def image_part(filename, cid, color, width=900):
    output = BytesIO()
    Image.new("RGB", (width, width // 2), color).save(output, format="PNG")
    part = EmailMessage()
    part.set_content(output.getvalue(), maintype="image", subtype="png")
    if filename:
        # Match the original newsletter: filename only in Content-Type.
        part.set_param("name", filename)
    part["Content-ID"] = f"<{cid}>"
    return part


class RelayImageTests(unittest.TestCase):
    def test_forwarding_removes_original_authentication_headers(self):
        message = EmailMessage()
        message["From"] = "sender@example.com"
        message["Message-ID"] = "<original@example.com>"
        stale_headers = (
            "DKIM-Signature", "DomainKey-Signature", "Authentication-Results",
            "ARC-Seal", "ARC-Message-Signature", "ARC-Authentication-Results",
        )
        for header in stale_headers:
            message[header] = "original-authentication"
        message["DKIM-Signature"] = "second-original-signature"
        message.set_content("Original newsletter")
        forwarded = BytesParser(policy=policy.default).parsebytes(
            relay.forward_full_fidelity(message.as_bytes(), "reader@example.com", "test")
        )
        for header in stale_headers:
            self.assertNotIn(header, forwarded)
        self.assertEqual(forwarded["Message-ID"], "<original@example.com>")
        self.assertIn("List-Unsubscribe", forwarded)

    def test_resize_preserves_filename_before_replacing_content_type(self):
        part = image_part("photo.png", "photo", "red")
        relay._resize_inline_images(part)
        self.assertEqual(part.get_content_type(), "image/jpeg")
        self.assertEqual(part.get_filename(), "photo.jpg")
        self.assertEqual(part.get_param("name"), "photo.jpg")
        self.assertEqual(part.get_content_disposition(), "inline")
        with Image.open(BytesIO(part.get_payload(decode=True))) as resized:
            self.assertEqual(resized.size, (600, 300))

    def test_round_trip_retains_distinct_images_and_matching_references(self):
        message = EmailMessage()
        message["From"] = "sender@example.com"
        message.set_content("Newsletter")
        message.add_alternative(
            '<p>News &amp; photos</p>\n'
            '<img src="cid:first" width="688" height="344" '
            'style="width:7.166in;height:3.583in;border:0">\n'
            '<img src="cid:second" width="390" height="195">\n'
            '<img src="cid:poster" width="427" height="213">',
            subtype="html",
        )
        alternative = message
        message = EmailMessage()
        message["From"] = "sender@example.com"
        message.make_related()
        message.attach(alternative)
        message.attach(image_part("photo.png", "first", "red"))
        message.attach(image_part("photo.jpg", "second", "blue"))
        poster = image_part("poster.png", "poster", "green", width=427)
        poster_bytes = poster.get_payload(decode=True)
        message.attach(poster)
        result = BytesParser(policy=policy.default).parsebytes(
            relay.forward_full_fidelity(message.as_bytes(), "reader@example.com", "test")
        )
        images = [part for part in result.walk() if part.get_content_maintype() == "image"]
        self.assertEqual(len(images), 3)
        self.assertEqual(len({part.get_filename() for part in images}), 3)
        self.assertEqual(len({hashlib.sha256(part.get_payload(decode=True)).digest()
                              for part in images}), 3)
        html = next(part.get_content() for part in result.walk()
                    if part.get_content_type() == "text/html")
        for part in images:
            self.assertEqual(part.get_content_disposition(), "inline")
            self.assertEqual(part.get_param("name"), part.get_filename())
            self.assertIn("cid:" + str(part["Content-ID"])[1:-1], html)
        self.assertEqual(images[-1].get_payload(decode=True), poster_bytes)
        self.assertIn("max-width:600px", html)
        self.assertIn("max-width:390px", html)
        self.assertNotIn("height=", html)
        self.assertNotIn("7.166in", html)
        self.assertIn("News &amp; photos", html)

    def test_missing_and_duplicate_filenames_are_unique(self):
        message = EmailMessage()
        message.make_related()
        for index, filename in enumerate([None, "same.png", "SAME.png", "same-1.png"]):
            message.attach(image_part(filename, f"photo-{index}", "red", width=100))
        relay._normalize_inline_content_ids(message)
        names = [part.get_filename().casefold() for part in message.iter_parts()]
        self.assertEqual(len(set(names)), 4)

    def test_responsive_html_preserves_surrounding_markup(self):
        before = '<!-- <img src="ignore"> -->\n<p title="a &amp; b">Hello&nbsp;</p>\n'
        tag = '<IMG SRC="cid:photo" WIDTH=390 HEIGHT=292 STYLE="color:red;min-width:900px" />'
        after = '\n<script>var text = "<img>";</script><p>End</p>'
        html = relay._ResponsiveImages(before + tag + after).render()
        self.assertTrue(html.startswith(before))
        self.assertTrue(html.endswith(after))
        self.assertIn('src="cid:photo"', html)
        self.assertIn("color:red;width:100%;max-width:390px;height:auto", html)
        self.assertNotIn("min-width", html)


if __name__ == "__main__":
    unittest.main()
