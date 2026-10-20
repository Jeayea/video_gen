"""剧集资产：角色 / 场景 / 关键道具。

资产的**唯一来源是故事圣经**（`characters` / `settings` / `props`），这里不另存一份：
- 出场章节从长篇大纲（`StoryChapter.characters/locations/props`）推出来
- 镜头用量从分镜（`character_in_frame` / `location` / `props_in_frame`）推出来
- 参考图落在 `refs/<id>.*`，路径登记在圣经条目的 `reference_image` 上

角色定妆照沿用 `character_image_service`；场景图（无人背景）与道具图（白底单品）在这里生成。
视频阶段 `video_service.resolve_references` 会把配了图的场景/道具一起送进视频模型。
"""

from __future__ import annotations

import glob
import logging
import re
from pathlib import Path

from videogen.config import Settings, get_settings
from videogen.models.bible import Prop, Setting, StoryBible
from videogen.providers.image import ImageProvider, get_image_provider
from videogen.providers.image.base import ImageProviderError, ImageRequest
from videogen.services import character_image_service, cost_service
from videogen.services.character_image_service import CharacterImageError
from videogen.utils.file_store import FileStore
from videogen.utils.redact import scrub_secrets

logger = logging.getLogger(__name__)

__all__ = [
    "ASSET_KINDS",
    "AssetError",
    "build_prop_prompt",
    "build_setting_prompt",
    "english_only",
    "generate_asset_image",
    "list_assets",
    "set_reference",
]

#: 资产类别（与圣经字段同名，也是 API 路径里的 kind）
ASSET_KINDS = ("characters", "settings", "props")
_ALLOWED_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
#: 图片模型会把 prompt 里的中文画进画面（定妆照踩过的坑），资产图 prompt 一律剔除中日韩字符
_CJK_RE = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿가-힯＀-￯]+")


class AssetError(CharacterImageError):
    """资产操作失败（消息已脱敏，可直接展示）。"""


def _items(bible: StoryBible, kind: str) -> list:
    if kind not in ASSET_KINDS:
        raise AssetError(f"未知的资产类别: {kind}（可选 {'/'.join(ASSET_KINDS)}）")
    return getattr(bible, kind)


#: 画面里本来就有的字：引号括起来、紧跟在「写着 / 招牌 / 显示」这类词后面
#: （`the blackboard reads "距离高考还有305天"`）。
#: 台词 / 旁白虽然也在引号里，但前面是 says / saying，不算
_SIGNAGE_RE = re.compile(
    r"\b(?:reads?|reading|written|writes|writing|displays?|displaying|shows?|showing|"
    r"characters|sign|signboard|signage|banner|slogan|labell?ed|titled|inscribed)\s*[:：]?\s*"
    r"(?:\"([^\"]+)\"|“([^”]+)”|「([^」]+)」)",
    re.IGNORECASE,
)


def signage_text(text: str | None) -> list[str]:
    """prompt 里要求画出来的中文（黑板 / 招牌 / 横幅上的原文），按出现顺序去重。"""
    found: list[str] = []
    for match in _SIGNAGE_RE.finditer(str(text or "")):
        quoted = next(group for group in match.groups() if group is not None).strip()
        if quoted and _CJK_RE.search(quoted) and quoted not in found:
            found.append(quoted)
    return found


def english_only(text: str | None, *, keep_signage: bool = False) -> str:
    """剔除中日韩字符（图片模型会把 prompt 里的中文画进画面：角色名、风格名被印成海报标题）。

    `keep_signage=True` 时，画面里本来就有的字（`signage_text`）原样保留 —— 黑板倒计时、
    网吧招牌是场景的一部分，删掉它们视频模型就只能自己编（《重回2000》编成了英文）。
    """
    raw = str(text or "")
    if not keep_signage:
        return " ".join(_CJK_RE.sub(" ", raw).split()).strip(" ,.;")
    out: list[str] = []
    last = 0
    for match in _SIGNAGE_RE.finditer(raw):
        quoted = next(group for group in match.groups() if group is not None)
        if not _CJK_RE.search(quoted):
            continue
        start = match.start(match.lastindex)
        out.append(_CJK_RE.sub(" ", raw[last:start - 1]))
        out.append(f' "{quoted.strip()}"')
        last = match.end()
    out.append(_CJK_RE.sub(" ", raw[last:]))
    return " ".join("".join(out).split()).strip(" ,.;")


def text_rule(prompt: str, *, default: str) -> str:
    """「不许有字」的那句：prompt 里有要画的中文时，改成「只许有这些字、照抄」。"""
    signs = signage_text(prompt)
    if not signs:
        return default
    quoted = ", ".join(f'"{sign}"' for sign in signs)
    return (
        f"The only visible writing is {quoted} in Simplified Chinese characters, written exactly "
        "as quoted, every stroke correct, clean and legible; no other text, no English words, "
        "no captions, no watermark"
    )


_english = english_only


def build_setting_prompt(setting: Setting, *, aspect_ratio: str = "9:16", extra: str = "") -> str:
    """场景参考图：**没有人**的背景板（人物交给角色图锁，场景图只锁环境）。"""
    look = english_only(setting.visual_prompt, keep_signage=True) or (
        "a detailed, lived-in real-world location"
    )
    lines = [
        f"Background plate of a single location with no people: {look}",
        "Wide establishing view, natural motivated lighting, rich environmental detail in "
        "foreground and background, photorealistic, cinematic",
        "Empty scene only — no people, no figures, no logo, no watermark, no border",
        text_rule(look, default="no text, no letters, no signs with words"),
        f"{aspect_ratio} composition",
    ]
    if _english(extra):
        lines.append(_english(extra))
    return "\n".join(lines)


def build_prop_prompt(prop: Prop, *, extra: str = "") -> str:
    """道具参考图：白底单品（只锁物品外观，不带剧情和场景）。"""
    look = english_only(prop.visual_prompt, keep_signage=True) or "a single everyday object"
    lines = [
        f"Product photograph of one single object: {look}",
        "Isolated on a plain pure white seamless background, centered, soft even studio "
        "lighting, photorealistic, sharp focus, true-to-life materials and wear",
        "Object only — no people, no hands, no logo, no watermark, no border",
        text_rule(look, default="no text, no letters, no labels"),
        "1:1 composition",
    ]
    if _english(extra):
        lines.append(_english(extra))
    return "\n".join(lines)


def _has_file(store: FileStore, project_id: str, relative: str) -> bool:
    return bool(relative) and store.resolve_artifact(project_id, relative) is not None


def list_assets(store: FileStore, project_id: str) -> dict[str, object]:
    """剧集资产总览：每个资产的出场章节、镜头用量、有没有参考图（只读，$0）。"""
    bible = store.load_bible(project_id)
    if bible is None:
        return {"characters": [], "settings": [], "props": [], "summary": {}}
    story = store.load_story_outline(project_id)
    storyboard = store.load_storyboard(project_id)

    chapters: dict[str, list[str]] = {}
    for chapter in story.chapters if story else []:
        for aid in [*chapter.characters, *chapter.locations, *chapter.props]:
            chapters.setdefault(aid, [])
            if chapter.id not in chapters[aid]:
                chapters[aid].append(chapter.id)
    shots: dict[str, int] = {}
    for shot in storyboard.shots if storyboard else []:
        used = {*shot.character_in_frame, *shot.props_in_frame}
        if shot.location:
            used.add(shot.location)
        for aid in used:
            shots[aid] = shots.get(aid, 0) + 1

    out: dict[str, object] = {}
    summary: dict[str, dict[str, int]] = {}
    for kind in ASSET_KINDS:
        rows = []
        for item in _items(bible, kind):
            rows.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "aliases": list(item.aliases),
                    "description": item.description,
                    "visual_prompt": item.visual_prompt,
                    "reference_image": item.reference_image,
                    "has_reference": _has_file(store, project_id, item.reference_image),
                    "chapters": chapters.get(item.id, []),
                    "shots": shots.get(item.id, 0),
                }
            )
        out[kind] = rows
        summary[kind] = {
            "total": len(rows),
            "with_reference": sum(1 for r in rows if r["has_reference"]),
        }
    out["summary"] = summary
    return out


async def set_reference(
    store: FileStore, project_id: str, kind: str, asset_id: str, relative: str
) -> bool:
    """把参考图路径写回圣经（锁内重新读一遍再改，只动这一个条目）。"""
    async with character_image_service._lock_for(project_id):
        bible = store.load_bible(project_id)
        if bible is None:
            return False
        items = _items(bible, kind)
        index = next((i for i, item in enumerate(items) if item.id == asset_id), None)
        if index is None:
            return False
        items[index] = items[index].model_copy(update={"reference_image": relative})
        store.save_bible(project_id, bible)
        return True


def clear_reference_files(
    store: FileStore, project_id: str, asset_id: str, keep: Path | None = None
) -> None:
    """删掉 refs/ 里这个资产的旧图（换图/删图时用；`keep` 是刚生成的那张）。"""
    for stale in store.refs_dir(project_id).glob(f"{glob.escape(asset_id)}.*"):
        if stale != keep and stale.suffix.lower() in _ALLOWED_SUFFIXES:
            stale.unlink(missing_ok=True)


async def generate_asset_image(
    store: FileStore,
    project_id: str,
    kind: str,
    asset_id: str,
    *,
    provider: ImageProvider | None = None,
    force: bool = False,
    prompt_override: str = "",
    settings: Settings | None = None,
) -> dict[str, object]:
    """给一个资产生成参考图；已有图默认跳过（force=true 重画）。角色交给定妆照服务。"""
    settings = settings or get_settings()
    if kind == "characters":
        return await character_image_service.generate_character_image(
            store,
            project_id,
            asset_id,
            provider=provider,
            force=force,
            prompt_override=prompt_override,
            settings=settings,
        )
    bible = store.load_bible(project_id)
    if bible is None:
        raise AssetError("故事圣经不存在，请先生成故事圣经")
    item = next((i for i in _items(bible, kind) if i.id == asset_id), None)
    if item is None:
        raise AssetError(f"资产不存在: {kind}/{asset_id}")
    if _has_file(store, project_id, item.reference_image) and not force:
        return {
            "asset_id": asset_id,
            "kind": kind,
            "name": item.name,
            "skipped": True,
            "reason": "已有参考图（force=true 可覆盖）",
            "reference_image": item.reference_image,
            "cost_usd": 0.0,
        }

    project = store.load_project(project_id)
    aspect = project.aspect_ratio.value if project else "9:16"
    if kind == "settings":
        prompt = _english(prompt_override) or build_setting_prompt(
            item, aspect_ratio=aspect, extra=settings.image_prompt_suffix
        )
    else:
        aspect = "1:1"
        prompt = _english(prompt_override) or build_prop_prompt(
            item, extra=settings.image_prompt_suffix
        )

    cost_service.check_budget(
        cost_service.load_report(store, project_id, settings),
        upcoming_usd=settings.image_price_usd,
        action=f"生成资产图（{item.name or asset_id}）",
    )
    own_provider = provider is None
    image_provider = provider or get_image_provider()
    target = store.refs_dir(project_id) / f"{asset_id}.png"
    request = ImageRequest(
        subject_id=asset_id,
        prompt=prompt,
        output_path=target,
        aspect_ratio=aspect,
        resolution=settings.image_resolution,
    )
    try:
        generated = await image_provider.generate(request)
    except ImageProviderError as exc:
        raise AssetError(f"资产图生成失败：{scrub_secrets(str(exc))[:300]}") from exc
    finally:
        if own_provider:
            await image_provider.aclose()

    saved = Path(generated.path)
    if not saved.is_file() or saved.stat().st_size == 0:
        raise AssetError("图片模型没有产出有效文件")
    clear_reference_files(store, project_id, asset_id, keep=saved)
    relative = f"refs/{saved.name}"
    await set_reference(store, project_id, kind, asset_id, relative)

    entry = cost_service.image_entry(
        image_provider.name,
        image_provider.model or settings.image_provider,
        1,
        price_usd=settings.image_price_usd,
        actual_usd=generated.actual_cost_usd,
    )
    cost_service.record(store, project_id, entry)
    logger.info(
        "生成资产图 project=%s %s/%s usd=%.4f", project_id, kind, asset_id, entry.usd
    )
    return {
        "asset_id": asset_id,
        "kind": kind,
        "name": item.name,
        "skipped": False,
        "reference_image": relative,
        "prompt": prompt,
        "cost_usd": entry.usd,
    }
