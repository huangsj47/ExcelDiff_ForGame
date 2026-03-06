from services.diff_service import DiffService


def test_cs_extension_is_treated_as_text():
    service = DiffService()
    assert service.get_file_type("Source/Foo/Bar.cs") == "text"

