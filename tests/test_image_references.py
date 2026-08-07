from __future__ import annotations

import unittest

from neuro_code.infrastructure.providers.image_references import (
    GEMINI_IMAGE_MEDIA_TYPES,
    GEMINI_MAX_INLINE_IMAGE_BYTES,
    InlineImageReference,
    RemoteImageReference,
    is_gemini_file_uri,
    parse_image_reference,
)


class ImageReferenceTests(unittest.TestCase):
    def test_inline_images_are_validated_and_media_type_aliases_are_normalized(self) -> None:
        reference = parse_image_reference(
            "data:image/jpg;base64,aW1hZ2U=",
            allowed_media_types=GEMINI_IMAGE_MEDIA_TYPES,
            max_inline_bytes=GEMINI_MAX_INLINE_IMAGE_BYTES,
        )

        self.assertEqual(
            reference,
            InlineImageReference(media_type="image/jpeg", data="aW1hZ2U="),
        )
        assert isinstance(reference, InlineImageReference)
        self.assertEqual(reference.data_uri, "data:image/jpeg;base64,aW1hZ2U=")

    def test_invalid_inline_images_are_rejected(self) -> None:
        values = (
            "data:image/png;base64,",
            "data:image/png,not-base64",
            "data:text/plain;base64,aW1hZ2U=",
            "data:image/png;base64,not-base64",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(
                    parse_image_reference(
                        value,
                        allowed_media_types=GEMINI_IMAGE_MEDIA_TYPES,
                        max_inline_bytes=GEMINI_MAX_INLINE_IMAGE_BYTES,
                    )
                )

        self.assertIsNone(
            parse_image_reference(
                "data:image/png;base64,YWI=",
                allowed_media_types=GEMINI_IMAGE_MEDIA_TYPES,
                max_inline_bytes=1,
            )
        )

    def test_remote_images_require_safe_http_urls(self) -> None:
        reference = parse_image_reference(
            "https://example.com/image.png?signature=fixture",
            allowed_media_types=GEMINI_IMAGE_MEDIA_TYPES,
            max_inline_bytes=GEMINI_MAX_INLINE_IMAGE_BYTES,
        )

        self.assertEqual(
            reference,
            RemoteImageReference("https://example.com/image.png?signature=fixture"),
        )
        for value in (
            "file:///tmp/image.png",
            "ftp://example.com/image.png",
            "https://user:secret@example.com/image.png",
            "https://[invalid/image.png",
        ):
            with self.subTest(value=value):
                self.assertIsNone(
                    parse_image_reference(
                        value,
                        allowed_media_types=GEMINI_IMAGE_MEDIA_TYPES,
                        max_inline_bytes=GEMINI_MAX_INLINE_IMAGE_BYTES,
                    )
                )

    def test_gemini_file_resources_are_distinguished_from_public_urls(self) -> None:
        file_reference = RemoteImageReference(
            "https://generativelanguage.googleapis.com/v1beta/files/file-1"
        )

        self.assertTrue(is_gemini_file_uri(file_reference))
        self.assertFalse(is_gemini_file_uri(RemoteImageReference("https://example.com/image.png")))


if __name__ == "__main__":
    unittest.main()
