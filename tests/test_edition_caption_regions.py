"""Source-grounded caption/image association must remain conservative."""
import base64
import io

import pytest
from PIL import Image
from reportlab.pdfgen.canvas import Canvas
from test_reading_projection import b, report

from paperlens.models import Box, ReadingItem, ReadingPart
from paperlens.reading import prepare_reading
from paperlens.reading_edition import EditionBatch, EditionBlock, apply_edition
from paperlens.reading_visual_ownership import caption_image_box


def scene():
    caption = b('caption', 'Figure 7: An embedded diagram.', kind='caption',
                box=(.1, .51, .5, .55), order=1)
    raster = Box(x0=.1, y0=.2, x1=.5, y1=.5)
    item = ReadingItem(id='f', kind='figure', page=1, label='Figure 7', bbox=caption.bbox,
                       parts=[ReadingPart(block_id=caption.id, end=len(caption.text))])
    return item, caption, raster


def test_unique_adjacent_native_image_extends_real_crop_and_owns_image_ocr(tmp_path):
    image = tmp_path / 'diagram.png'
    Image.new('RGB', (120, 80), 'navy').save(image)
    pdf = tmp_path / 'diagram.pdf'
    canvas = Canvas(str(pdf), pagesize=(600, 800))
    canvas.drawImage(str(image), 60, 400, 240, 240)
    canvas.drawString(60, 380, 'Figure 7: An embedded diagram.')
    canvas.save()
    _, caption, _ = scene()
    value = report([b('ocr', 'BUTTON choice A choice B', box=(.12, .25, .45, .45), text_source='ocr'), caption])
    original = value.document.model_dump()
    prepare_reading(value)
    apply_edition(value, [EditionBatch(items=[EditionBlock(
        kind='figure', page=1, label='Figure 7', box=(.1, .51, .5, .55),
        source_ids=['caption'], content='An embedded diagram.',
    )])], pdf)
    assert [item.kind for item in value.reading.items] == ['figure']
    figure = value.reading.items[0]
    # The caption stays in structured text; its printed copy must not be in the crop.
    assert figure.bbox == Box(x0=.1, y0=.2, x1=.5, y1=.5)
    assert figure.content == 'An embedded diagram.'
    assert {part.block_id for part in figure.parts} == {'ocr', 'caption'}
    assert value.document.model_dump() == original
    with Image.open(io.BytesIO(base64.b64decode(figure.asset.split(',', 1)[1]))) as crop:
        red, green, blue = crop.getpixel((crop.width // 2, crop.height // 3))
        assert blue > 100 and red < 20 and green < 20


def test_table_only_batch_still_reads_native_image_regions_and_owns_image_ocr(tmp_path):
    image = tmp_path / 'matrix.png'
    Image.new('RGB', (120, 80), 'navy').save(image)
    pdf = tmp_path / 'matrix.pdf'
    canvas = Canvas(str(pdf), pagesize=(600, 800))
    canvas.drawImage(str(image), 60, 400, 240, 240)
    canvas.save()
    value = report([b('ocr', 'INPUT OUTPUT 15 20', box=(.12, .25, .45, .45), text_source='ocr')])
    original = value.document.model_dump()
    prepare_reading(value)
    apply_edition(value, [EditionBatch(items=[EditionBlock(
        kind='table', page=1, box=(.1, .2, .5, .5),
        head=[['Input', 'Output']], rows=[['15', '20']],
    )])], pdf)
    assert [item.kind for item in value.reading.items] == ['table']
    table = value.reading.items[0]
    assert table.asset
    assert {part.block_id for part in table.parts} == {'ocr'}
    assert [cell.text for cell in table.table.cells] == ['Input', 'Output', '15', '20']
    assert value.document.model_dump() == original
    assert value.reading.structured_pages == [1]


def test_ambiguous_images_are_not_attached():
    item, caption, raster = scene()
    second = raster.model_copy(update={'y1': .495})
    assert caption_image_box(item, caption, {caption.id: caption}, {1: [raster, second]}) is None


def test_interposed_prose_is_not_swallowed():
    item, caption, raster = scene()
    prose = b('body', 'Independent text.', box=(.1, .502, .5, .508))
    assert caption_image_box(item, caption, {caption.id: caption, prose.id: prose}, {1: [raster]}) is None


def test_two_neighboring_captions_make_the_image_ambiguous():
    item, caption, raster = scene()
    another = b('other', 'Figure 6: Another diagram.', kind='caption', box=(.1, .15, .5, .19))
    assert caption_image_box(item, caption, {caption.id: caption, another.id: another}, {1: [raster]}) is None


@pytest.mark.parametrize('label', ['', 'Figure 8', 'Table 7'])
def test_unverified_number_cannot_select_an_image(label):
    item, caption, raster = scene()
    item.label = label
    assert caption_image_box(item, caption, {caption.id: caption}, {1: [raster]}) is None


def test_an_uncertain_broad_model_box_does_not_select_an_adjacent_image():
    item, caption, raster = scene()
    item.bbox = Box(x0=.05, y0=.49, x1=.95, y1=.8)
    assert caption_image_box(item, caption, {caption.id: caption}, {1: [raster]}) is None


def test_remote_image_does_not_select_an_existing_caption():
    item, caption, raster = scene()
    raster = raster.model_copy(update={'y1': .45})
    assert caption_image_box(item, caption, {caption.id: caption}, {1: [raster]}) is None


def test_a_neighboring_small_illustration_does_not_match_a_wide_caption():
    item, caption, raster = scene()
    raster = raster.model_copy(update={'x0': .2, 'x1': .32})
    assert caption_image_box(item, caption, {caption.id: caption}, {1: [raster]}) is None


def test_unmatched_caption_transcription_keeps_the_original_ocr_fallback(monkeypatch, tmp_path):
    _, caption, raster = scene()
    value = report([b('ocr', 'BUTTON choice A choice B', box=(.12, .25, .45, .45), text_source='ocr'), caption])
    prepare_reading(value)
    monkeypatch.setattr('paperlens.reading_edition.pdf_image_regions', lambda *_: {1: [raster]})
    monkeypatch.setattr('paperlens.reading_edition.page_image', lambda *_: 'caption-image')
    apply_edition(value, [EditionBatch(items=[EditionBlock(
        kind='figure', page=1, label='Figure 7', box=(.1, .51, .5, .55),
        source_ids=['caption'], content='A different ungrounded description.',
    )])], tmp_path / 'source.pdf')
    figure = next(item for item in value.reading.items if item.kind == 'figure')
    assert figure.bbox == caption.bbox
    assert {part.block_id for part in figure.parts} == {'caption'}
    assert any(item.kind == 'paragraph' and any(part.block_id == 'ocr' for part in item.parts)
               for item in value.reading.items)
    assert value.reading.structured_pages == []


@pytest.mark.parametrize('marker', ['$^7$', '$^{7}$'])
def test_inline_superscript_format_variants_do_not_repeat_the_native_word(marker):
    value = report([b('body', 'An accompanying tool7 performs the check.')])
    prepare_reading(value)
    apply_edition(value, [EditionBatch(items=[EditionBlock(
        kind='paragraph', page=1, source_ids=['body'],
        content=f'An accompanying tool{marker} performs the check.',
    )])])
    assert len(value.reading.items) == 1
    assert value.reading.structured_pages == [1]


def test_wrong_superscript_number_is_not_assumed_to_match():
    value = report([b('body', 'An accompanying tool7 performs the check.')])
    prepare_reading(value)
    apply_edition(value, [EditionBatch(items=[EditionBlock(
        kind='paragraph', page=1, source_ids=['body'],
        content='An accompanying tool$^9$ performs the check.',
    )])])
    assert value.reading.structured_pages == []
    assert any(item.content is None for item in value.reading.items)
