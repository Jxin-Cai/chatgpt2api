from __future__ import annotations

from io import BytesIO
from typing import Any, Iterator

from PIL import Image

from services.protocol.conversation import (
    ConversationRequest,
    ImageGenerationError,
    collect_image_outputs,
    count_text_tokens,
    encode_images,
    stream_image_chunks,
    stream_image_outputs_with_pool,
)
from utils.helper import DEFAULT_IMAGE_MODEL
from utils.image_tokens import count_image_inputs_tokens, count_image_output_items_tokens, image_usage


def _composite_mask(
    images: list[tuple[bytes, str, str]],
    masks: list[tuple[bytes, str, str]],
) -> list[tuple[bytes, str, str]]:
    """Apply a single mask to the first image; retain paired-mask extension."""
    if not masks:
        return images
    if not images:
        raise ImageGenerationError("image is required", status_code=400)
    if len(masks) not in {1, len(images)}:
        raise ImageGenerationError("provide one mask or one mask per image", status_code=400)
    result = list(images)
    for i, (mask_data, _mask_filename, _mask_mime) in enumerate(masks):
        data, filename, _mime_type = images[i]
        try:
            with Image.open(BytesIO(data)) as source, Image.open(BytesIO(mask_data)) as mask:
                if mask.size != source.size:
                    raise ImageGenerationError("mask must have the same dimensions as image", status_code=400)
                img = source.convert("RGBA")
                # Preserve grayscale-mask extension; honor alpha even in palette PNGs.
                if "A" in mask.getbands() or "transparency" in mask.info:
                    alpha = mask.convert("RGBA").getchannel("A")
                elif mask.mode == "L":
                    alpha = mask.copy()
                else:
                    raise ImageGenerationError("mask must have an alpha channel or be grayscale", status_code=400)
                img.putalpha(alpha)
                buf = BytesIO()
                img.save(buf, format="PNG")
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise ImageGenerationError("invalid image or mask data", status_code=400) from exc
        result[i] = (buf.getvalue(), filename.rsplit(".", 1)[0] + ".png", "image/png")
    return result


def handle(body: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
    prompt = str(body.get("prompt") or "")
    images = body.get("images") or []
    masks = body.get("mask") or []
    images = _composite_mask(images, masks)
    model = str(body.get("model") or DEFAULT_IMAGE_MODEL)
    n = int(body.get("n") or 1)
    size = body.get("size")
    quality = str(body.get("quality") or "auto")
    response_format = str(body.get("response_format") or "b64_json")
    base_url = str(body.get("base_url") or "") or None
    progress_callback = body.get("progress_callback")
    encoded_images = encode_images(images)
    if not encoded_images:
        raise ImageGenerationError("image is required")
    outputs = stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        n=n,
        size=size,
        quality=quality,
        response_format=response_format,
        base_url=base_url,
        images=encoded_images,
        message_as_error=True,
        progress_callback=progress_callback,
    ))
    if body.get("stream"):
        return stream_image_chunks(outputs)
    result = collect_image_outputs(outputs)
    result["usage"] = image_usage(
        input_text_tokens=count_text_tokens(prompt, model),
        input_image_tokens=count_image_inputs_tokens(images, model),
        output_tokens=count_image_output_items_tokens(result.get("data"), size, quality),
    )
    return result
