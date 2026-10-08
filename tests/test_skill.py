"""The drum-style skill's grooves must parse with the notation it documents."""
import re
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "ableton-drums"
VOICES = {"K", "S", "X", "H", "O", "P", "R", "B", "C", "N", "T1", "T2", "T3", "F"}
LINE = re.compile(r"^(K|S|X|H|O|P|R|B|C|N|T1|T2|T3|F)\s+([xXogfr.|]+)(\s|$)")


def groove_blocks(text):
    for block in re.findall(r"```\n(.*?)```", text, re.S):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if lines and all(LINE.match(ln) or ln.lstrip().startswith(("step", "#")) for ln in lines):
            yield [LINE.match(ln) for ln in lines if LINE.match(ln)]


def test_skill_frontmatter_and_references_exist():
    head = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert head.startswith("---\nname: ableton-drums\ndescription: ")
    for name in re.findall(r"`references/[^`]+`", head):
        path = name.strip("`")
        if "{" in path:  # references/genres/{a, b}.md
            base, opts = re.match(r"(.*)\{(.*)\}\.md", path).groups()
            for opt in opts.split(","):
                assert (SKILL / f"{base}{opt.strip()}.md").exists(), opt
        else:
            assert (SKILL / path).exists(), path


def test_every_groove_has_consistent_bar_length():
    checked = 0
    for md in SKILL.rglob("*.md"):
        text = md.read_text(encoding="utf-8")
        for block in groove_blocks(text):
            lengths = {len(m.group(2).replace("|", "")) for m in block}
            assert len(lengths) == 1, f"{md.name}: mixed lengths {lengths}"
            assert lengths.pop() in (12, 16, 18), f"{md.name}: odd bar length"
            assert {m.group(1) for m in block} <= VOICES
            checked += 1
    assert checked >= 30
