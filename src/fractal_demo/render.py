"""Float64 Mandelbrot tiles with a shared, continuous escape-time color scale."""

from io import BytesIO

import numpy as np
from PIL import Image

from .protocol import RenderRequest

PALETTES = [
    {"id": "cyber", "label": "Cyan / violet"},
    {"id": "fire", "label": "Amber / crimson"},
]

_COLOR_STOPS = {
    "cyber": ((18, 25, 65), (33, 205, 225), (201, 239, 249), (136, 67, 213), (18, 25, 65)),
    "fire": ((45, 8, 24), (181, 25, 57), (251, 159, 35), (255, 239, 179), (45, 8, 24)),
}
_INTERIOR = (10, 13, 20)
_COLOR_PERIOD = 32.0


def render_tile(request: RenderRequest) -> bytes:
    """Render a validated request as an RGB PNG without ancillary metadata."""
    # Add the global integer offset before scaling, identically for every tile.
    x = request.xmin + (request.px + np.arange(request.width)) * request.pixel_size
    y = request.ymin + (request.py + np.arange(request.height)) * request.pixel_size
    cr, ci = (axis.ravel() for axis in np.meshgrid(x, y))
    zr = np.zeros(cr.size, dtype=np.float64)
    zi = np.zeros(cr.size, dtype=np.float64)
    active = np.ones(cr.size, dtype=bool)
    smooth = np.zeros(cr.size, dtype=np.float64)

    # Tiny finite coordinates may underflow. Escaped points leave the loop
    # immediately, and smoothing never evaluates interior points (including 0).
    with np.errstate(over="ignore", under="ignore", invalid="ignore", divide="ignore"):
        for iteration in range(1, request.iterations + 1):
            indices = np.flatnonzero(active)
            if indices.size == 0:
                break
            real, imag = zr[indices], zi[indices]
            next_real = real * real - imag * imag + cr[indices]
            next_imag = 2.0 * real * imag + ci[indices]
            zr[indices], zi[indices] = next_real, next_imag
            magnitude_squared = next_real * next_real + next_imag * next_imag
            escaped = magnitude_squared > 4.0
            escaped_indices = indices[escaped]
            # n + 1 - log2(log(|z_n|)); log(|z|) = log(|z|**2) / 2.
            smooth[escaped_indices] = (
                iteration + 1.0 - np.log2(0.5 * np.log(magnitude_squared[escaped]))
            )
            active[escaped_indices] = False

    rgb = np.empty((cr.size, 3), dtype=np.uint8)
    rgb[:] = _INTERIOR
    escaped = ~active
    phase = np.remainder(smooth[escaped] / _COLOR_PERIOD, 1.0)
    stops = np.asarray(_COLOR_STOPS[request.palette], dtype=np.float64)
    positions = np.linspace(0.0, 1.0, len(stops))
    for channel in range(3):
        rgb[escaped, channel] = np.floor(np.interp(phase, positions, stops[:, channel]))

    output = BytesIO()
    image = Image.fromarray(rgb.reshape(request.height, request.width, 3))
    image.save(output, format="PNG", compress_level=1)
    return output.getvalue()
