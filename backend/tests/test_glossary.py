from app.glossary import load_glossary_block


def test_none_path_returns_empty_string():
    assert load_glossary_block(None) == ""


def test_missing_file_returns_empty_string():
    assert load_glossary_block("config/does-not-exist.yaml") == ""


def test_loads_string_and_dict_terms(tmp_path):
    yaml_file = tmp_path / "glossary.yaml"
    yaml_file.write_text(
        "terms:\n"
        "  - Kubernetes\n"
        "  - term: hydration\n"
        "    note: no traducir\n",
        encoding="utf-8",
    )
    block = load_glossary_block(str(yaml_file))
    assert "- Kubernetes" in block
    assert "- hydration (no traducir)" in block


def test_term_without_note_has_no_parentheses(tmp_path):
    yaml_file = tmp_path / "glossary.yaml"
    yaml_file.write_text("terms:\n  - term: Vercel\n", encoding="utf-8")
    block = load_glossary_block(str(yaml_file))
    assert block == "- Vercel"


def test_relative_path_resolves_against_project_root_not_cwd(monkeypatch, tmp_path):
    # simulate running from an unrelated cwd (e.g. `cd backend && uvicorn ...`)
    monkeypatch.chdir(tmp_path)
    block = load_glossary_block("config/glossary.example.yaml")
    assert "Kubernetes" in block
