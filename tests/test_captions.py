from __future__ import annotations

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
    assert result.html.endswith("<blockquote>这是简介</blockquote>")
    assert "演员" not in result.plain


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


def test_html_is_escaped_and_intro_is_limited_in_utf16_units():
    result = build_caption(
        "简介：<b>😀😀😀</b>",
        [],
        [],
        limit=7,
    )

    assert result.plain == "<b>😀😀"
    assert result.html == "<blockquote>&lt;b&gt;😀😀</blockquote>"


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
