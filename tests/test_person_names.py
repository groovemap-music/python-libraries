import pytest

from common.person_names import strip_person_name


@pytest.mark.parametrize(
    "padding",
    [
        " ",
        "\t",
        "\n",
        "\r\n",
        "\u00a0",  # no-break space
        "\u2003",  # em space
        "\u3000",  # ideographic space
        "\u2028",  # line separator
        "\u2029",  # paragraph separator
        "\u0000",  # control
        "\u200b",  # zero width space
        "\u200c",  # zero width non-joiner (measured)
        "\u200d",  # zero width joiner
        "\u200e",  # left-to-right mark
        "\u200f",  # right-to-left mark (measured)
        "\u2060",  # word joiner
        "\ufeff",  # byte order mark (measured)
    ],
)
def test_strips_padding_at_both_ends(padding: str) -> None:
    assert strip_person_name(f"{padding}Flood{padding}") == "Flood"
    assert strip_person_name(f"Flood{padding}{padding}{padding}") == "Flood"
    assert strip_person_name(f"{padding}Flood") == "Flood"


def test_strips_mixed_padding() -> None:
    assert strip_person_name(" \u200fFlood\ufeff\u200c ") == "Flood"


def test_unpadded_name_is_returned_unchanged() -> None:
    assert strip_person_name("Flood") == "Flood"
    assert strip_person_name("Mercedita López") == "Mercedita López"


@pytest.mark.parametrize(
    "name",
    ["Mercedita" + " " * 12 + "López", "A\u200fB", "A\u200cB", "A\ufeffB", "A\tB", "A\u00a0B"],
)
def test_interior_characters_are_unchanged(name: str) -> None:
    assert strip_person_name(f" {name}\u200f") == name


@pytest.mark.parametrize("name", ["", " ", "   ", "\u200f", "\ufeff\u200c \t", "\u200f\u200f\u200f"])
def test_all_padding_name_is_empty(name: str) -> None:
    assert strip_person_name(name) == ""


def test_is_idempotent() -> None:
    once = strip_person_name(" \u200fA B\u200f ")
    assert strip_person_name(once) == once == "A B"


def test_matches_str_strip_on_plain_whitespace() -> None:
    for name in (" Flood ", "\tFlood\n", "  A  B  "):
        assert strip_person_name(name) == name.strip()
