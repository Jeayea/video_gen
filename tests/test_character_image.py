from __future__ import annotations

import pytest

from tests.factories import small_project
from tests.fakes import FakeImageProvider
from videogen.models.cost import CostStage
from videogen.providers.image.base import ImageProviderError, ImageRequest
from videogen.providers.image.mock import MockImageProvider, _solid_png
from videogen.services import character_image_service, cost_service
from videogen.services.character_image_service import (
    CharacterImageError,
    build_portrait_prompt,
    reference_plan,
)
from videogen.services.cost_service import BudgetExceeded

# ── prompt 组装 ────────────────────────────────────────


def test_portrait_prompt_carries_appearance_and_neutral_framing(bible):
    character = bible.get_character("char_1")
    assert character is not None
    prompt = build_portrait_prompt(character, bible, aspect_ratio="1:1")
    assert character.visual_prompt.split(",")[0] in prompt
    assert "reference portrait" in prompt
    assert "Front-facing head-and-shoulders portrait" in prompt
    # 定妆照必须干净：不能带剧照的光影/场景/文字
    assert "no text" in prompt
    assert "1:1" in prompt


def test_portrait_prompt_keeps_only_the_base_look():
    """人设里的「后期逆袭/换造型」不能进定妆照，否则模型会画成一个模糊的中间态。"""
    from videogen.models.bible import Character
    from videogen.services.character_image_service import _base_look

    character = Character(
        id="c",
        name="林悦",
        visual_prompt=(
            "A young woman with a shy demeanor in plain clothes. "
            "Later, she transforms into a confident supermodel."
        ),
    )
    prompt = build_portrait_prompt(character)
    assert "shy demeanor in plain clothes" in prompt
    assert "supermodel" not in prompt  # 弧光部分被截掉

    # 第一句就带转折时保留原文（宁可多描述，不能没描述）
    assert _base_look("Later, everything changes") == "Later, everything changes"
    assert _base_look("A girl in plain clothes") == "A girl in plain clothes"


def test_portrait_prompt_falls_back_to_description():
    from videogen.models.bible import Character

    character = Character(id="c", name="某人", description="一个戴眼罩的男人")
    prompt = build_portrait_prompt(character)
    assert "一个戴眼罩的男人" in prompt


# ── 单角色生成 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_character_image_writes_file_and_bible(store, bible):
    project = small_project(store)
    store.save_bible(project.id, bible)
    provider = FakeImageProvider()

    result = await character_image_service.generate_character_image(
        store, project.id, "char_1", provider=provider
    )

    assert result["skipped"] is False
    assert result["reference_image"] == "refs/char_1.png"
    path = store.resolve_artifact(project.id, "refs/char_1.png")
    assert path is not None and path.stat().st_size > 0
    # 圣经被写回，后续视频生成才能拿到参考图
    assert store.load_bible(project.id).get_character("char_1").reference_image == "refs/char_1.png"
    # 传进去的请求带着角色设定
    assert provider.requests and provider.requests[0].subject_id == "char_1"
    assert "char_1" not in provider.requests[0].prompt  # 用名字而不是 id


@pytest.mark.asyncio
async def test_generate_character_image_records_cost_with_actual_usd(store, bible):
    project = small_project(store)
    store.save_bible(project.id, bible)

    await character_image_service.generate_character_image(
        store, project.id, "char_1", provider=FakeImageProvider(actual_cost_usd=0.0375)
    )

    report = store.load_cost_report(project.id)
    image_entries = [e for e in report.entries if e.stage == CostStage.IMAGE]
    assert len(image_entries) == 1
    assert image_entries[0].usd == 0.0375  # 以 provider 实报为准
    assert image_entries[0].unit_kind == "image"
    assert image_entries[0].calls == 1


@pytest.mark.asyncio
async def test_generate_character_image_falls_back_to_configured_price(store, bible, settings):
    project = small_project(store)
    store.save_bible(project.id, bible)

    await character_image_service.generate_character_image(
        store, project.id, "char_1", provider=FakeImageProvider(actual_cost_usd=None)
    )

    report = store.load_cost_report(project.id)
    entry = next(e for e in report.entries if e.stage == CostStage.IMAGE)
    assert entry.usd == settings.image_price_usd


@pytest.mark.asyncio
async def test_existing_reference_is_skipped_unless_forced(store, bible):
    project = small_project(store)
    store.save_bible(project.id, bible)
    first = FakeImageProvider()

    await character_image_service.generate_character_image(
        store, project.id, "char_1", provider=first
    )
    second = FakeImageProvider()
    skipped = await character_image_service.generate_character_image(
        store, project.id, "char_1", provider=second
    )
    assert skipped["skipped"] is True
    assert second.requests == []  # 没花钱
    assert skipped["cost_usd"] == 0.0

    forced = FakeImageProvider()
    result = await character_image_service.generate_character_image(
        store, project.id, "char_1", provider=forced, force=True
    )
    assert result["skipped"] is False
    assert len(forced.requests) == 1


@pytest.mark.asyncio
async def test_reference_image_is_passed_as_img2img_input_when_asked(store, bible):
    project = small_project(store)
    store.save_bible(project.id, bible)
    await character_image_service.generate_character_image(
        store, project.id, "char_1", provider=FakeImageProvider()
    )

    provider = FakeImageProvider()
    await character_image_service.generate_character_image(
        store, project.id, "char_1", provider=provider, force=True, use_reference=True
    )
    assert provider.requests[0].reference_image is not None


@pytest.mark.asyncio
async def test_missing_character_raises(store, bible):
    project = small_project(store)
    store.save_bible(project.id, bible)
    with pytest.raises(CharacterImageError):
        await character_image_service.generate_character_image(
            store, project.id, "char_missing", provider=FakeImageProvider()
        )


@pytest.mark.asyncio
async def test_budget_blocks_generation(store, bible, monkeypatch, settings):
    project = small_project(store)
    store.save_bible(project.id, bible)
    monkeypatch.setattr(settings, "budget_usd", 0.01)  # 一张 $0.04 的图就超了

    with pytest.raises(BudgetExceeded):
        await character_image_service.generate_character_image(
            store, project.id, "char_1", provider=FakeImageProvider()
        )
    assert store.resolve_artifact(project.id, "refs/char_1.png") is None


# ── 批量 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_all_only_fills_missing_and_isolates_failures(store, bible):
    project = small_project(store)
    store.save_bible(project.id, bible)
    # 先给 char_1 一张，批量时应该跳过它
    await character_image_service.generate_character_image(
        store, project.id, "char_1", provider=FakeImageProvider()
    )

    provider = FakeImageProvider(fail_subjects={"char_3"})
    result = await character_image_service.generate_all_references(
        store, project.id, provider=provider
    )

    assert result["skipped"] == 1
    assert result["failed"] == 1
    assert result["generated"] == len(bible.characters) - 2
    # char_4 也必须有图：一个角色失败不能拖垮其它角色
    assert store.load_bible(project.id).get_character("char_4").reference_image
    errors = [r for r in result["results"] if r.get("error")]
    assert len(errors) == 1 and errors[0]["char_id"] == "char_3"


@pytest.mark.asyncio
async def test_generate_all_estimates_then_records_total(store, bible):
    project = small_project(store)
    store.save_bible(project.id, bible)
    plan = reference_plan(store, project.id)
    assert plan["missing"] == len(bible.characters)
    assert plan["estimated_usd"] == pytest.approx(len(bible.characters) * 0.04)

    result = await character_image_service.generate_all_references(
        store, project.id, provider=FakeImageProvider(actual_cost_usd=0.04)
    )
    assert result["cost_usd"] == pytest.approx(len(bible.characters) * 0.04)
    assert reference_plan(store, project.id)["missing"] == 0


# ── provider / 存储细节 ────────────────────────────────


@pytest.mark.asyncio
async def test_mock_image_provider_writes_a_real_png(tmp_path):
    provider = MockImageProvider()
    output = tmp_path / "char.png"
    generated = await provider.generate(
        ImageRequest(subject_id="char_1", prompt="x", output_path=output)
    )
    raw = generated.path.read_bytes()
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    assert generated.actual_cost_usd is None  # mock 不报花费
    # 不同角色 -> 配色不同（肉眼可分辨，测试也能断言）
    outputs = set()
    for subject in ("char_1", "char_2", "char_3", "char_4"):
        item = await provider.generate(
            ImageRequest(subject_id=subject, prompt="x", output_path=tmp_path / f"{subject}.png")
        )
        outputs.add(item.path.read_bytes())
    assert len(outputs) >= 2


def test_solid_png_is_structurally_valid():
    raw = _solid_png(4, 4, (10, 20, 30))
    assert raw.startswith(b"\x89PNG\r\n\x1a\n") and raw.endswith(b"IEND\xaeB`\x82")


@pytest.mark.asyncio
async def test_jpeg_suffix_from_provider_is_kept(store, bible):
    """provider 返回 jpg 时，圣经里的路径要跟着变（不能写死 .png）。"""
    project = small_project(store)
    store.save_bible(project.id, bible)
    result = await character_image_service.generate_character_image(
        store, project.id, "char_1", provider=FakeImageProvider(suffix=".jpg")
    )
    assert result["reference_image"] == "refs/char_1.jpg"
    assert not (store.refs_dir(project.id) / "char_1.png").exists()


@pytest.mark.asyncio
async def test_provider_failure_becomes_character_image_error(store, bible):
    project = small_project(store)
    store.save_bible(project.id, bible)

    class Boom(FakeImageProvider):
        async def generate(self, request: ImageRequest):
            self.requests.append(request)
            raise ImageProviderError("上游 500")

    with pytest.raises(CharacterImageError, match="角色图生成失败"):
        await character_image_service.generate_character_image(
            store, project.id, "char_1", provider=Boom()
        )


def test_estimate_image_usd_uses_settings(settings):
    assert cost_service.estimate_image_usd(4) == pytest.approx(4 * settings.image_price_usd)
