from app.export import FORMATTERS, to_srt, to_txt, to_vtt
from app.providers.base import TranscriptEvent


def _event(start_ms=0, end_ms=1000, source="Hello world.", translations=None, source_lang="en"):
    return TranscriptEvent(
        utterance_id="u1",
        source_text=source,
        source_lang=source_lang,
        translations=translations or {"es": "Hola mundo."},
        is_final=True,
        start_ms=start_ms,
        end_ms=end_ms,
    )


def test_srt_timestamp_format_and_numbering():
    entries = [_event(0, 1500), _event(2000, 4321)]
    srt = to_srt(entries, lang="es")
    assert srt.startswith("1\n00:00:00,000 --> 00:00:01,500\nHola mundo.")
    assert "\n2\n00:00:02,000 --> 00:00:04,321\nHola mundo." in srt


def test_vtt_uses_dot_separator_and_header():
    vtt = to_vtt([_event(0, 1500)], lang="es")
    assert vtt.startswith("WEBVTT\n")
    assert "00:00:00.000 --> 00:00:01.500" in vtt
    assert "," not in vtt.split("\n", 2)[1]  # no SRT-style comma timestamps


def test_txt_is_just_the_lines():
    entries = [_event(source="One.", translations={"es": "Uno."}), _event(source="Two.", translations={"es": "Dos."})]
    assert to_txt(entries, lang="es") == "Uno.\nDos."


def test_lang_none_returns_source_text():
    entries = [_event(source="Hello.", translations={"es": "Hola."})]
    assert to_txt(entries, lang=None) == "Hello."


def test_lang_equal_to_source_returns_source_text():
    entries = [_event(source_lang="en", source="Hello.", translations={"es": "Hola."})]
    assert to_txt(entries, lang="en") == "Hello."


def test_missing_translation_falls_back_to_source_text():
    entries = [_event(source="Hello.", translations={"es": "Hola."})]
    # asking for a language that was never translated (e.g. degraded translation)
    assert to_txt(entries, lang="pt") == "Hello."


def test_formatters_registry_has_all_three_formats():
    assert set(FORMATTERS) == {"srt", "vtt", "txt"}
