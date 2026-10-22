"""科普「手账拼贴」画风：资料卡（原文红框）+ 手绘配图 + 手写批注，全部离线验证。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_explainer import _with_storyboard, requires_ffmpeg
from videogen.models.bible import Fact, StoryBible
from videogen.models.project import AspectRatio, ContentType, Project
from videogen.models.storyboard import Shot
from videogen.orchestrator.errors import PipelineError
from videogen.providers.image.base import ImageProvider
from videogen.providers.image.mock import MockImageProvider
from videogen.providers.video import still as still_provider
from videogen.providers.video.base import VideoRequest
from videogen.services import collage_service, explainer_service, subtitle_service, video_service

MATERIAL = """## A Survey of Self-Improving Systems (arXiv:2607.07663)

Systems increasingly improve themselves. We order the signals into a verification hierarchy
from formal verifiers (strongest) to intrinsic self-assessment (weakest). Strength tracks it.

## Another Paper (arXiv:2603.08640)

Agents sometimes engage in reward hacking: training on the test set."""
QUOTE = (
    "order the signals into a verification hierarchy from formal verifiers (strongest) "
    "to intrinsic self-assessment (weakest)"
)


def _project(**kwargs) -> Project:
    defaults = dict(
        title="RSI",
        content_type=ContentType.EXPLAINER,
        source_material=MATERIAL,
        visual_mode="still",
        illustration_style="notebook",
        aspect_ratio=AspectRatio("16:9"),
    )
    return Project(**{**defaults, **kwargs})


def _bible(source: str = QUOTE) -> StoryBible:
    return StoryBible(facts=[Fact(id="fact_1", content="验证信号有强弱", source=source)])


def _shot(**kwargs) -> Shot:
    return Shot(id="scene_2_shot_1", scene_id="scene_2", shot_no=1, **kwargs)


def test_excerpt_boxes_the_quote_and_takes_the_nearest_heading():
    excerpt = collage_service.find_excerpt(MATERIAL, QUOTE, context_chars=40)
    assert excerpt is not None
    # 资料里的换行在卡片上是空格；框住的正好是原句
    assert excerpt.text[excerpt.quote_start : excerpt.quote_end] == QUOTE
    assert excerpt.title == "A Survey of Self-Improving Systems (arXiv:2607.07663)"
    # 前文从句子开头起，不从半个词开始；下一篇论文的标题不会混进来
    assert excerpt.text.startswith("… We order")
    assert "Another Paper" not in excerpt.text
    later = collage_service.find_excerpt(MATERIAL, "training on the test set")
    assert later is not None and later.title == "Another Paper (arXiv:2603.08640)"


def test_spec_uses_first_locatable_fact_and_skips_self_sourced_ones():
    bible = StoryBible(facts=[
        Fact(id="fact_1", content="模型说的", source=explainer_service.SOURCE_SELF),
        Fact(id="fact_2", content="有出处", source=QUOTE),
    ])
    spec = collage_service.build_spec(
        _shot(fact_refs=["fact_1", "fact_2"], annotation="“裁判越硬，走得越远”"), _project(), bible
    )
    assert spec.has_card and spec.excerpts[0].text.count("verification hierarchy") == 1
    assert spec.annotation == "裁判越硬，走得越远"  # 引号去掉
    assert collage_service.doodle_aspect(spec) == "1:1"  # 有资料卡时配图只占一侧
    bare = collage_service.build_spec(_shot(fact_refs=["fact_1"]), _project(), bible)
    assert not bare.has_card and collage_service.doodle_aspect(bare) == "16:9"
    # 没有参考资料：一律没有资料卡
    empty = _project(source_material="")
    assert not collage_service.build_spec(_shot(fact_refs=["fact_2"]), empty, bible).has_card


def test_layout_keeps_brackets_with_their_word():
    pytest.importorskip("PIL")
    font = collage_service._font("", 20)
    words = [w for line in collage_service._layout(QUOTE, font, 10_000) for w, _ in line]
    assert "(strongest)" in words and "(" not in words


def _red_pixels(image, box) -> int:
    region = image.crop(box)
    return sum(1 for r, g, b in region.get_flattened_data() if r > 180 and g < 90 and b < 90)


@pytest.mark.parametrize("aspect", ["16:9", "9:16"])
def test_render_draws_card_with_red_box_doodle_and_note(tmp_path, aspect):
    image_mod = pytest.importorskip("PIL.Image")
    doodle = image_mod.new("RGB", (512, 512), "white")
    doodle.paste((0, 0, 0), (200, 200, 312, 312))  # 一块黑色「线稿」
    doodle_path = tmp_path / "doodle.png"
    doodle.save(doodle_path)
    project = _project(aspect_ratio=AspectRatio(aspect))
    spec = collage_service.build_spec(_shot(fact_refs=["fact_1"], annotation="这也行？"),
                                      project, _bible())
    out = collage_service.render(spec, doodle_path, tmp_path / "frame.png")
    frame = image_mod.open(out).convert("RGB")
    assert frame.size == collage_service.CANVAS_SIZES[aspect]
    w, h = frame.size
    card_area = (0, 0, int(w * 0.6), h) if w > h else (0, 0, w, int(h * 0.47))
    assert _red_pixels(frame, card_area) > 200  # 原文红框
    # 配图正片叠底：黑色线稿还是黑的，配图的白底变成纸色（不是纯白）
    assert min(sum(p) for p in frame.get_flattened_data()) < 60
    probe = (int(w * 0.78), int(h * 0.3)) if w > h else (w // 2, int(h * 0.58))
    assert frame.getpixel(probe) != (255, 255, 255)
    assert all(abs(c - p) < 12 for c, p in zip(frame.getpixel(probe), collage_service.PAPER,
                                                  strict=True))


def test_render_without_card_or_note_has_no_red(tmp_path):
    image_mod = pytest.importorskip("PIL.Image")
    image_mod.new("RGB", (640, 360), "white").save(tmp_path / "d.png")
    spec = collage_service.build_spec(_shot(), _project(), _bible())
    frame = image_mod.open(collage_service.render(spec, tmp_path / "d.png", tmp_path / "f.png"))
    assert _red_pixels(frame.convert("RGB"), (0, 0, *frame.size)) == 0


class _AspectImage(ImageProvider):
    name = "fake-image"
    model = "fake-image-1"

    def __init__(self):
        self.aspects: list[str] = []

    async def generate(self, request):
        self.aspects.append(request.aspect_ratio)
        return await MockImageProvider().generate(request)

    async def aclose(self):
        return None


@requires_ffmpeg
async def test_still_provider_composes_the_frame_and_pushes_in_gently(tmp_path, monkeypatch):
    image = _AspectImage()
    provider = still_provider.StillVideoProvider(image, price_usd=0.04, fps=12)
    composed: list[Path] = []

    async def compose(doodle: Path) -> Path:
        composed.append(doodle)
        return doodle

    filters: list[str] = []
    real_run = still_provider.run_ffmpeg

    async def spy(args, **kwargs):
        filters.append(args[args.index("-vf") + 1])
        return await real_run(args, **kwargs)

    monkeypatch.setattr(still_provider, "run_ffmpeg", spy)
    request = VideoRequest(shot_id="s1", prompt="p", still_prompt="a doodle", duration_sec=2,
                           aspect_ratio="16:9", output_path=tmp_path / "s1.mp4",
                           compose_still=compose, still_aspect="1:1", still_zoom=0.035)
    await provider.generate(request)
    assert image.aspects == ["1:1"] and len(composed) == 1
    assert "z='1+0.035*" in filters[0]  # 只轻推，不平移


async def test_notebook_storyboard_keeps_annotations_other_styles_drop_them(store, make_pipeline):
    _, _, llm, storyboard = await _with_storyboard(
        store, make_pipeline, visual_mode="still", illustration_style="notebook"
    )
    assert all(shot.annotation.startswith("批注") for shot in storyboard.shots)
    visuals = next(c for c in llm.calls if "科普画面分镜师" in str(c[0].content))
    assert "手账拼贴画风" in str(visuals[-1].content)
    _, _, llm2, comic = await _with_storyboard(
        store, make_pipeline, visual_mode="still", illustration_style="comic"
    )
    assert all(shot.annotation == "" for shot in comic.shots)
    visuals = next(c for c in llm2.calls if "科普画面分镜师" in str(c[0].content))
    assert "手账拼贴画风" not in str(visuals[-1].content)


def test_notebook_still_prompt_drops_scene_background(settings):
    from videogen.models.bible import Setting

    bible = StoryBible(settings=[Setting(id="loc_1", name="实验室",
                                         visual_prompt="a busy laboratory with glass walls")])
    shot = _shot(video_prompt="a referee holding a whistle", location="loc_1")
    notebook = video_service.still_prompt(shot, _project(), bible)
    assert "laboratory" not in notebook and "marker doodle" in notebook
    other = video_service.still_prompt(shot, _project(illustration_style="comic"), bible)
    assert "laboratory" in other


def test_annotation_only_changes_the_signature_when_set():
    plain = _shot(video_prompt="x")
    assert len(video_service.shot_signature(plain)) == len(video_service._VIDEO_FIELDS)
    noted = plain.model_copy(update={"annotation": "这也行？"})
    assert video_service.shot_signature(noted) != video_service.shot_signature(plain)
    # 资料卡印的是 fact_refs 里那条的原句：拼贴镜头改了引用要重拼，别的镜头照旧
    recited = noted.model_copy(update={"fact_refs": ["fact_9"]})
    assert video_service.shot_signature(recited) != video_service.shot_signature(noted)
    other = plain.model_copy(update={"fact_refs": ["fact_9"]})
    assert video_service.shot_signature(other) == video_service.shot_signature(plain)


async def test_missing_pillow_fails_the_shot_before_buying_the_doodle(
    store, make_pipeline, monkeypatch
):
    project, pipeline, _, storyboard = await _with_storyboard(
        store, make_pipeline, visual_mode="still", illustration_style="notebook"
    )
    monkeypatch.setattr(collage_service, "unavailable_reason", lambda: "没装 Pillow")
    drawn: list[str] = []

    async def no_draw(self, request):
        drawn.append(request.subject_id)
        raise AssertionError("不该买图")

    monkeypatch.setattr(MockImageProvider, "generate", no_draw)
    await pipeline.confirm_storyboard(project.id, force=True)
    job = await pipeline.start_video_generation(project.id)
    with pytest.raises(PipelineError, match="全部"):
        await pipeline.jobs.wait(job.id)
    manifest = store.load_video_manifest(project.id)
    assert drawn == []
    assert [c.error for c in manifest.clips] == ["没装 Pillow"] * len(storyboard.shots)


def test_notebook_projects_default_to_handwritten_subtitles():
    name, look = subtitle_service.subtitle_look("note")
    assert name == "note" and look.font == "ZCOOL KuaiLe"
    created = explainer_service.apply_creation_defaults(_project(subtitle_style=""))
    assert created.subtitle_style == "note"
    other = explainer_service.apply_creation_defaults(_project(illustration_style="comic"))
    assert other.subtitle_style == explainer_service.SUBTITLE_STYLE


def test_excerpt_unfolds_pdf_ligatures():
    """PDF 抽出来的 `ﬀ` 衬线字体里没有字形（RSI 试拍里印成了「o□icial」）。"""
    material = "## P\n\nAgents lag behind oﬃcial models. The best agent scores 23.2% here."
    excerpt = collage_service.find_excerpt(material, "The best agent scores 23.2% here")
    assert excerpt is not None and "official" in excerpt.text and "ﬃ" not in excerpt.text
    boxed = excerpt.text[excerpt.quote_start : excerpt.quote_end]
    assert boxed == "The best agent scores 23.2% here"


def test_card_shows_the_fact_the_sentence_is_about():
    """旁白先引了背景事实、再引真正在说的那条：资料卡放后者（RSI 试拍里框错了 3 镜）。"""
    bible = StoryBible(facts=[
        Fact(id="fact_3", content="有界自我微调已成为工业实践", source=QUOTE),
        Fact(id="fact_7", content="智能体在函数调用测试中达到 89% 对比 67%",
             source="training on the test set"),
    ])
    said = "AI 在函数调用上反超官方，89% 对比 67%。"
    shot = _shot(fact_refs=["fact_3", "fact_7"], narration=said)
    spec = collage_service.build_spec(shot, _project(), bible)
    assert spec.excerpts[0].title == "Another Paper (arXiv:2603.08640)"


def test_excerpt_stops_before_the_next_section_heading():
    material = "## P · 1. Intro\n\nFirst we test the idea here.\n\n## P · 2. Setup\n\nThen more."
    excerpt = collage_service.find_excerpt(material, "First we test the idea here")
    assert excerpt is not None and "Setup" not in excerpt.text and "##" not in excerpt.text
    assert not excerpt.text.endswith("…")


def test_annotation_voice_follows_the_tone():
    """批注的口吻跟着语气走：玩梗可以吐槽，个人解读 / 严谨只点要点（PostTrainBench 嫌太口语）。"""
    assert "吐槽" in explainer_service.notebook_rules(_project(tone="meme"))
    plain = explainer_service.notebook_rules(_project(tone="commentary"))
    assert "吐槽" not in plain and "不用感叹号" in plain
    assert explainer_service.notebook_rules(_project(illustration_style="comic")) == ""
