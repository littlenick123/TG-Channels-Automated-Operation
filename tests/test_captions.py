from __future__ import annotations

from telethon.extensions import html as telethon_html
from telethon.tl.types import MessageEntityBlockquote, MessageEntityBold

from channel_operator.captions import build_caption


class FirstRandom:
    def sample(self, population, count):
        return list(population[:count])


class LastRandom:
    def sample(self, population, count):
        return list(reversed(population))[:count]


def test_caption_prioritizes_present_keep_tags_and_drops_forbidden():
    source = "#甲 #必留 #删除 #乙 #丙 #丁 #戊\n简介：这是简介\n演员：不保留"

    result = build_caption(
        source,
        ["#必留", "#不存在"],
        ["#删除"],
        random_source=FirstRandom(),
    )

    assert result.tags == ("#必留", "#甲", "#乙", "#丙", "#丁")
    assert "#删除" not in result.plain
    assert result.plain.endswith("这是简介")
    assert result.html.endswith("<blockquote expandable>这是简介</blockquote>")
    assert "演员" not in result.plain


def test_tag_line_is_bold_and_has_no_blank_line_before_intro():
    result = build_caption("标签：#甲 #乙\n简介：简介内容", [], [], random_source=FirstRandom())

    assert result.plain == "#甲 #乙\n简介内容"
    assert result.html == (
        "<b>#甲 #乙</b>\n"
        "<blockquote expandable>简介内容</blockquote>"
    )
    parsed_text, entities = telethon_html.parse(result.html)
    assert parsed_text == result.plain
    assert len(entities) == 2
    assert isinstance(entities[0], MessageEntityBold)
    assert isinstance(entities[1], MessageEntityBlockquote)
    assert entities[1].collapsed is True


def test_intro_footer_is_bold_inside_the_same_collapsed_quote():
    result = build_caption(
        "标签：#甲\n简介：原始简介",
        [],
        [],
        intro_footer="固定追加内容",
        random_source=FirstRandom(),
    )

    assert result.plain == "#甲\n原始简介\n固定追加内容"
    assert result.html == (
        "<b>#甲</b>\n"
        "<blockquote expandable>原始简介\n<b>固定追加内容</b></blockquote>"
    )
    parsed_text, entities = telethon_html.parse(result.html)
    assert parsed_text == result.plain
    assert sum(isinstance(entity, MessageEntityBold) for entity in entities) == 2
    assert sum(isinstance(entity, MessageEntityBlockquote) for entity in entities) == 1


def test_intro_footer_becomes_intro_when_source_intro_is_missing():
    result = build_caption(
        "演员：不会保留",
        [],
        [],
        intro_footer="固定内容 <&>",
    )

    assert result.tags == ()
    assert result.plain == "固定内容 <&>"
    assert result.intro == "固定内容 <&>"
    assert result.html == (
        "<blockquote expandable><b>固定内容 &lt;&amp;&gt;</b></blockquote>"
    )


def test_intro_is_truncated_before_footer_so_footer_is_preserved():
    result = build_caption(
        "简介：1234567890",
        [],
        [],
        intro_footer="页尾",
        limit=9,
    )

    assert result.plain == "123...\n页尾"
    assert result.html.endswith("123...\n<b>页尾</b></blockquote>")
    assert len(result.plain.encode("utf-16-le")) // 2 <= 9


def test_each_new_attempt_may_choose_a_different_random_set():
    source = "#一 #二 #三 #四 #五 #六 #七\n简介：内容"

    first = build_caption(source, [], [], random_source=FirstRandom())
    last = build_caption(source, [], [], random_source=LastRandom())

    assert first.tags != last.tags
    assert len(first.tags) == len(last.tags) == 5


def test_large_keep_library_still_outputs_only_first_five_present_tags():
    keep_tags = [f"#必留{index}" for index in range(20)]
    source = " ".join(reversed(keep_tags)) + "\n简介：内容"

    result = build_caption(source, keep_tags, [], random_source=LastRandom())

    assert result.tags == tuple(keep_tags[:5])


def test_missing_intro_and_too_few_tags_are_allowed():
    result = build_caption("#一 #二\n其他：不会保留", [], [])

    assert set(result.tags) == {"#一", "#二"}
    assert result.intro is None
    assert "其他" not in result.plain


def test_html_is_escaped_and_truncated_intro_ends_with_ellipsis():
    result = build_caption(
        "简介：<b>😀😀😀</b>",
        [],
        [],
        limit=7,
    )

    assert result.plain == "<b>..."
    assert result.html == "<blockquote expandable>&lt;b&gt;...</blockquote>"


def test_long_caption_stays_within_limit_and_marks_omitted_content():
    result = build_caption(
        "标签：#保留\n简介：" + "内容" * 600,
        [],
        [],
        limit=1024,
        random_source=FirstRandom(),
    )

    assert result.plain.endswith("...")
    assert len(result.plain.encode("utf-16-le")) // 2 <= 1024
    assert result.html.endswith("...</blockquote>")


def test_intro_uses_collapsed_telegram_blockquote_entity():
    result = build_caption("简介：这是一段可折叠简介", [], [])

    parsed_text, entities = telethon_html.parse(result.html)

    assert parsed_text == "这是一段可折叠简介"
    assert len(entities) == 1
    assert isinstance(entities[0], MessageEntityBlockquote)
    assert entities[0].collapsed is True


def test_only_first_hashtag_leading_line_is_used():
    result = build_caption("普通 #忽略\n#保留 #也保留\n简介: 文本", [], [])

    assert set(result.tags) == {"#保留", "#也保留"}


def test_labelled_tag_line_is_recognized_and_mentions_are_ignored():
    source = "标签：#中文字幕 #Wifey #欧美精选 #欧美剧情 @kakasp\n简介：影片简介"

    result = build_caption(source, [], [], random_source=FirstRandom())

    assert result.tags == ("#中文字幕", "#Wifey", "#欧美精选", "#欧美剧情")
    assert "@kakasp" not in result.plain


def test_ascii_colon_and_multiple_tag_lines_are_supported():
    source = "标签: #甲 #乙\n#丙 #丁\n简介：内容"

    result = build_caption(source, [], [], random_source=FirstRandom())

    assert result.tags == ("#甲", "#乙", "#丙", "#丁")
