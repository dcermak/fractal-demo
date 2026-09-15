import math
from dataclasses import replace
from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from fractal_demo.protocol import RenderRequest
from fractal_demo.render import PALETTES, render_tile


def request(**changes):
    base = RenderRequest(
        xmin=-2.0,
        ymin=-1.2,
        pixel_size=0.15,
        px=0,
        py=0,
        width=21,
        height=17,
        iterations=120,
        palette="cyber",
        expected_process_id="00000000-0000-4000-8000-000000000001",
    )
    return replace(base, **changes)


def decode(tile):
    with Image.open(BytesIO(render_tile(tile))) as image:
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.size == (tile.width, tile.height)
        assert image.info == {}
        return np.array(image)


def scalar_pixel(x, y, iterations, palette):
    """Independent complex-number orbit and scalar piecewise-linear palette."""
    z = 0j
    for n in range(1, iterations + 1):
        z = z * z + complex(x, y)
        if abs(z) > 2:
            escape_time = n + 1 - math.log(math.log(abs(z)), 2)
            # Each color segment spans eight iterations; the fourth returns
            # to the first color. These fixture colors are independent of the
            # production lookup table and NumPy interpolation.
            if palette == "cyber":
                colors = [(18, 25, 65), (33, 205, 225), (201, 239, 249), (136, 67, 213)]
            else:
                colors = [(45, 8, 24), (181, 25, 57), (251, 159, 35), (255, 239, 179)]
            position = (escape_time / 8) % 4
            index = math.floor(position)
            fraction = position - index
            start, end = colors[index], colors[(index + 1) % 4]
            return tuple(
                math.floor(a + (b - a) * fraction) for a, b in zip(start, end, strict=True)
            )
    return (10, 13, 20)


@pytest.mark.parametrize("palette", ["cyber", "fire"])
@pytest.mark.parametrize("iterations", [1, 2, 120])
def test_decoded_pixels_match_independent_scalar_oracle(palette, iterations):
    tile = request(palette=palette, iterations=iterations)
    pixels = decode(tile)
    expected = [
        [
            scalar_pixel(
                tile.xmin + (tile.px + column) * tile.pixel_size,
                tile.ymin + (tile.py + row) * tile.pixel_size,
                iterations,
                palette,
            )
            for column in range(tile.width)
        ]
        for row in range(tile.height)
    ]
    np.testing.assert_array_equal(pixels, expected)


@pytest.mark.parametrize("palette", ["cyber", "fire"])
@pytest.mark.parametrize("x,y", [(0.0, 0.0), (-1.0, 0.0), (-2.0, 0.0), (0.0, 1.0)])
def test_known_bounded_orbits_are_interior(palette, x, y):
    pixels = decode(request(xmin=x, ymin=y, width=1, height=1, palette=palette))
    assert tuple(pixels[0, 0]) == (10, 13, 20)


@pytest.mark.parametrize("palette", ["cyber", "fire"])
def test_escape_on_last_iteration_is_colored_and_budget_independent(palette):
    # c=1 has orbit 1, 2, 5: equality at radius two does not escape.
    tile = request(xmin=1.0, ymin=0.0, width=1, height=1, palette=palette)
    assert tuple(decode(replace(tile, iterations=2))[0, 0]) == (10, 13, 20)
    escaped = decode(replace(tile, iterations=3))
    assert tuple(escaped[0, 0]) == scalar_pixel(1.0, 0.0, 3, palette)
    assert tuple(escaped[0, 0]) != (10, 13, 20)
    np.testing.assert_array_equal(escaped, decode(replace(tile, iterations=500)))


@pytest.mark.parametrize("palette", ["cyber", "fire"])
@pytest.mark.parametrize(
    "xmin,ymin,step,px,py",
    [(-2.0, -1.2, 0.035, 11, 13), (-0.751, 0.099, 0.000031, 19, 23)],
)
def test_adjacent_horizontal_and_vertical_tiles_match_whole(palette, xmin, ymin, step, px, py):
    whole = request(
        xmin=xmin, ymin=ymin, pixel_size=step, px=px, py=py,
        width=24, height=18, iterations=350, palette=palette,
    )
    pixels = decode(whole)
    horizontal = np.concatenate(
        [decode(replace(whole, width=9)), decode(replace(whole, px=px + 9, width=15))],
        axis=1,
    )
    vertical = np.concatenate(
        [decode(replace(whole, height=7)), decode(replace(whole, py=py + 7, height=11))],
        axis=0,
    )
    np.testing.assert_array_equal(horizontal, pixels)
    np.testing.assert_array_equal(vertical, pixels)


@pytest.mark.parametrize("palette", ["cyber", "fire"])
@pytest.mark.parametrize(
    "changes",
    [
        dict(xmin=-4.0, ymin=-4.0, pixel_size=4.0, width=3, height=3),
        dict(xmin=4.0, ymin=4.0, width=1, height=1, iterations=10000),
        dict(xmin=-32764.0, ymin=-32764.0, pixel_size=4.0, px=8190, py=8190),
        dict(xmin=-0.75, ymin=0.1, pixel_size=1e-13, px=8190, py=8190),
        dict(xmin=1e-300, ymin=-1e-300, pixel_size=1e-13),
    ],
)
def test_extreme_valid_finite_samples_without_numerical_warnings(palette, changes):
    tile = replace(request(width=2, height=2, palette=palette), **changes)
    with np.errstate(all="raise"):
        pixels = decode(tile)
    for row in range(tile.height):
        for column in range(tile.width):
            expected = scalar_pixel(
                tile.xmin + (tile.px + column) * tile.pixel_size,
                tile.ymin + (tile.py + row) * tile.pixel_size,
                tile.iterations,
                palette,
            )
            assert tuple(pixels[row, column]) == expected


def test_palette_catalog():
    assert PALETTES == [
        {"id": "cyber", "label": "Cyan / violet"},
        {"id": "fire", "label": "Amber / crimson"},
    ]
