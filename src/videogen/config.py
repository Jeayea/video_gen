from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from videogen.utils import region

_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    llm_provider: str = "openrouter"
    openrouter_api_key: str = Field(default="", repr=False)
    openrouter_model: str = "google/gemini-2.5-flash"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_timeout_sec: float = 120.0
    llm_max_retries: int = 2
    llm_json_retries: int = 2

    # Video
    #: mock = 免费占位片段；openrouter = 真实视频模型（按秒计费）
    video_provider: str = "mock"
    #: OpenRouter 的视频模型（minimax/hailuo-3 = MiniMax H3；也支持 hailuo-3-max 等）
    openrouter_video_model: str = "minimax/hailuo-3"
    #: 分辨率交给模型能力校验，不支持会自动换成第一个支持的（H3 只支持 2K）
    video_resolution: str = ""
    #: 是否让视频模型自带音轨（我们用 TTS 配音，默认关）
    video_generate_audio: bool = False
    #: 角色一致性：把角色参考图传给视频模型
    video_use_reference_images: bool = True
    video_poll_interval_sec: float = 10.0
    #: 单个片段最多等多久（真实模型通常 1-5 分钟）
    video_timeout_sec: float = 900.0
    #: 合并短镜头时的目标最短时长（秒）。0 = 自动用模型最短计费档位（H3 是 5 秒）
    video_min_shot_sec: float = 0.0
    #: 分镜阶段单个镜头的目标时长（秒）。0 = 自动取最短档位 × 1.6（H3 → 约 8 秒）
    video_shot_target_sec: float = 0.0
    #: 单个镜头的时长上限（秒）。0 = 用模型最长档位（H3 = 15 秒）
    video_max_shot_sec: float = 0.0
    #: 段落分镜：同一场相邻镜头编成一段（段内硬切）一次生成，生成后按镜头切回单镜片段。
    #: 一段只买一次最短档位，2-4 秒的短镜头不再各自被抬到 5 秒计费
    video_segment_mode: bool = True
    #: 每段目标 / 上限秒数（上限还会被模型最长档位压住）/ 每段最多几个镜头
    video_segment_target_sec: float = 12.0
    video_segment_max_sec: float = 15.0
    video_segment_max_shots: int = 4
    #: 切回单镜时，把切点吸附到画面里真实的硬切（±1 秒内）；关掉就严格按计划时间切
    video_segment_snap_cuts: bool = True
    video_concurrency: int = 3
    #: 追加到每个视频 prompt 的「表演与连贯性」规范。真实样本里模型会把哭戏演成舞台剧
    #: （眉毛挤成一团、身体僵硬），也会在长 prompt 下变形；这段是统一的最低要求
    video_performance_directive: str = (
        "natural subtle expressions and micro-expressions, relaxed body language, "
        "natural blinking and breathing, smooth continuous motion, stable framing, "
        "single continuous take with no cuts, no morphing or warping, "
        "avoid exaggerated theatrical acting"
    )
    #: 追加到每个视频 prompt 的风格约束（英文；留空 = 不追加）
    video_style_prompt: str = ""
    #: 追加到每个视频 prompt 的「背景丰富度」规范。真实样本里分镜只写一句
    #: "in an office"，模型就把背景画成一面空墙 —— 这是「背景不够丰富」的直接原因
    video_background_directive: str = (
        "richly detailed lived-in environment, layered foreground midground background, "
        "practical props and set dressing, visible textures and materials, "
        "motivated directional lighting, atmospheric depth of field, "
        "no empty blank walls, no plain studio void"
    )
    #: 是否把台词写进视频 prompt。视频模型会按同一句话演口型；不写它就自己编一段话
    #: 在说（嘴按它自己的话动），我们再叠 TTS 上去 —— 口型必然对不上
    video_prompt_dialogue: bool = True
    video_max_retries: int = 2
    video_retry_backoff_sec: float = 1.5
    video_fps: int = 24
    video_crf: int = 30

    # TTS（配音）
    tts_provider: str = "edge"  # edge | openrouter | mock
    #: 云端配音模型（`TTS_PROVIDER=openrouter` 时用）。gpt-audio-mini 支持用自然语言描述念法
    openrouter_tts_model: str = "openai/gpt-audio-mini"
    #: 云端配音的落盘格式。**只能是 wav**：流式接口只返回 pcm16，我们自己封 WAV 头
    tts_audio_format: str = "wav"
    #: 配音参照视频原声（`off` | `native`）。`native`：视频模型自己念过一遍台词
    #: （需要 `VIDEO_GENERATE_AUDIO=true`），从原声里找出这句话的起止，云端 TTS 听着原声
    #: 用角色自己的音色重念（节奏/停顿/情绪照原声），时长微调到与原声一致后放回原位置
    audio_dub_guide: str = "off"
    #: 重念的配音与原声时长不一致时，变速（不变调）的最大倍率；超出部分不再硬拉
    audio_dub_fit_max_ratio: float = 1.25
    #: 没有逐条念法时的兜底念法（只对云端 TTS 生效）
    tts_default_instruction: str = (
        "自然的中文口语，像真人对话，句间有呼吸和停顿，不要播音腔"
    )
    #: 云端 TTS 的时长守卫：实际时长超过「按原文估算时长 × 该倍数」就判定模型自己加戏。
    #: 真实样本：一句「薇儿，为什么？」被某个音色念成 15.15 秒独白（还念出了括号提示）
    tts_max_duration_ratio: float = 2.0
    #: 默认音色；角色音色由 voice_map 决定，这里是兜底
    tts_voice: str = "zh-CN-XiaoxiaoNeural"
    tts_voice_female: str = "zh-CN-XiaoxiaoNeural"
    tts_voice_male: str = "zh-CN-YunxiNeural"
    tts_voice_narrator: str = "zh-CN-YunjianNeural"
    tts_rate: str = "+0%"
    tts_volume: str = "+0%"
    tts_pitch: str = "+0Hz"
    tts_concurrency: int = 3
    tts_max_retries: int = 2
    tts_retry_backoff_sec: float = 1.5
    #: 允许给角色选方言音色（例如东北话/陕西话）。默认不允许：同一部戏里混口音很出戏，
    #: 宁可「复用通用音色 + 音高区分」也不给主角上方言
    tts_allow_accent_voices: bool = False
    # 剧本严格模式：结构硬伤（空场/重复场/结尾悬着/缺冲突/LLM 评审报 error）会阻止「确认剧本」，
    # 需要 force=true 才能强制确认
    script_strict: bool = True
    #: 生成剧本后让 LLM 当一次剧本医生（结构/因果/动机），多花一次 LLM 调用（≈$0.001）
    script_llm_review: bool = True
    #: 长篇剧集大纲：默认章数 / 单批扩写章数（一次写 20 章正文必被截断，所以要分批）
    story_chapters: int = 20
    story_chapter_chunk: int = 4
    #: 小说模式：没有章标题时按多少字切一段 / 单章超过多少字再拆成几部分
    novel_chunk_chars: int = 5000
    novel_chapter_max_chars: int = 12000
    #: 小说模式：逐章抽取的并发数 / 生成圣经时先抽前几章（其余章选中展开时再增量抽取）
    novel_extract_concurrency: int = 4
    novel_initial_chapters: int = 3
    #: 小说模式：圣经最多收几个角色 / 每章最多抽几个关键道具
    novel_max_characters: int = 12
    novel_max_props_per_chapter: int = 3
    tts_timeout_sec: float = 60.0
    #: 每句对白后面留的呼吸时间（秒），用于音画对齐
    audio_gap_sec: float = 0.25
    #: 逐条配音响度归一（EBU R128）：逐条配音是独立生成的，音色不同、响度天然不一致
    #: （真实项目实测极差 7.9dB）。开启后在合成时按条补一个**有上限的**定值增益
    audio_normalize: bool = True
    #: 配音的目标集成响度（LUFS）
    audio_target_lufs: float = -20.0
    #: 单条最多修正多少 dB。超过这个范围的差异通常是有意的表演（耳语/嘶吼），不该拉平。
    #: 10dB 是实测定下来的：真实项目里最安静的那个音色比主角低 15dB，
    #: 6dB 的上限修完还差 6dB（能听出来），10dB 只剩 2dB（听不出来）
    audio_normalize_max_db: float = 10.0
    #: 配音比镜头长时的最大容忍倍率，超过只告警不裁音频
    audio_align_max_ratio: float = 2.0

    # 配音文案（可选：让 LLM 看着真实画面写/改台词，会额外花 LLM 额度）
    #: off = 只用分镜里的原文案；narration = 只给没台词的镜头补旁白；
    #: polish = 只把已有文案改得更口语/更贴画面；full = 补旁白 + 改写 + 念法建议
    audio_llm_script: str = "off"
    #: 是否把视频抽帧发给模型（关掉 = 纯文本，便宜但对不上画面）
    audio_llm_vision: bool = True
    #: 每个镜头抽几帧（1-3）；帧越多越准也越贵
    audio_llm_frames_per_shot: int = 1
    #: 抽帧缩放后的宽度（像素），越小越便宜
    audio_llm_frame_width: int = 640
    #: 配音文案单独指定模型（留空 = 用当前 LLM 模型）
    audio_llm_model: str = ""
    #: 单句文案的硬上限字符数，超了截断（防止模型写论文）
    audio_llm_max_chars: int = 60
    #: 对已经生成过文案的镜头再跑一次（默认跳过，避免重复花钱）
    audio_llm_force: bool = False
    #: 把「画面运动实测」（本地算，免费）也喂给配音导演：动得最猛的时段给最重的语气
    audio_llm_motion: bool = True
    #: 运动实测的采样帧率
    audio_motion_fps: int = 6

    # BGM（背景音乐）
    bgm_enabled: bool = True
    #: 配乐引擎：
    #:   library  = 本地曲库按情绪挑一首（现成的曲子，人工准备）
    #:   generate = **本机按分镜现写一首**（段落对齐镜头、动作爆发给重音、说话处让路，$0）
    #:   cli      = 外部音乐模型（MusicGen / Stable Audio 等，走 bgm_cli_cmd 模板）
    #:   auto     = 曲库有歌就用曲库，没有就现场写
    #:   none     = 不配乐
    bgm_engine: str = "library"
    bgm_dir: Path = Path("./assets/bgm")
    bgm_gain_db: float = -20.0
    bgm_fade_sec: float = 1.5
    #: 手动指定曲目 id（留空 = 按镜头情绪自动匹配）
    bgm_id: str = ""
    #: 人声闪避：有人说话时自动把音乐压下去（sidechaincompress），说完再抬回来
    bgm_duck: bool = True
    #: 闪避强度（压缩比）、触发门限（dBFS）、起停时间（毫秒）
    #: 门限别设太高：sidechaincompress 的压降 ≈ (人声高出多少 dB) × (1-1/ratio)，
    #: 实测 -30dBFS 只能压 4dB（几乎听不出来），-40dBFS 约 9dB
    bgm_duck_ratio: float = 6.0
    bgm_duck_threshold_db: float = -40.0
    bgm_duck_attack_ms: float = 25.0
    bgm_duck_release_ms: float = 420.0

    # 配乐生成（BGM_ENGINE=generate 时生效）
    #: 强制情绪色调（tense/sad/warm/hopeful/action/mystery/playful/neutral；留空 = 按分镜情绪）
    music_mood: str = ""
    #: 强制速度（0 = 按色调）
    music_bpm: int = 0
    #: 用户的配乐要求（自由文本，会参与情绪判断），例：「压抑一点，结尾要有希望」
    music_note: str = ""
    #: 混响湿量 0..1（<0 = 按情绪自动：悲伤/神秘更大，动作片更干）
    music_reverb: float = -1.0
    #: 让 LLM 给一次配乐建议（段落/色调/能量）。**付费**（约 $0.001），默认关
    music_llm_brief: bool = False
    #: 外部音乐模型命令模板，必须含 {out} 与 {prompt}（{duration} 可选）
    bgm_cli_cmd: str = ""
    bgm_cli_timeout_sec: float = 1200.0
    #: 追加给外部音乐模型命令的参数（空格分隔）
    bgm_cli_args: str = ""

    # 合成
    #: soft=mp4 内嵌 mov_text 字幕轨（不需要 libass）| burn=烧进画面 | none=只导出 srt
    compose_subtitle_mode: str = "soft"
    #: 成片整体响度归一（EBU R128）：把「这条片子整体多响」也定死
    #: （真实成片实测：不加是 -21.9 LUFS，短剧平台普遍按 -14~-16 LUFS 播放）
    compose_loudness_normalize: bool = True
    compose_target_lufs: float = -16.0
    #: 真峰值上限（dBTP）
    compose_true_peak_db: float = -1.5
    compose_crf: int = 23
    compose_preset: str = "veryfast"
    #: 成片变速（1.0 = 原速，2.0 = 2 倍速快放，0.5 = 慢放）。范围 0.5~4。
    #: 用途：视频模型按**最短档位**计费（H3 = 5 秒），所以 5 秒镜头最划算 ——
    #: 排 25 个 5 秒镜头再整体 2 倍速，成片 62.5 秒、每镜观感 2.5 秒，档位一分不浪费。
    #: 变速放在**最后一道 pass**（混音 + 响度归一 + 烧字幕之后），配音/口型/BGM/
    #: 烧进画面的字幕一起被缩放，因此仍然同步；音频用 `atempo`（变速不变调）。
    #: **内嵌软字幕（soft）的时间戳不参与变速**，所以变速时会自动改烧字幕。
    compose_speed: float = Field(default=1.0, ge=0.5, le=4.0)
    compose_concurrency: int = 2
    compose_subtitle_font: str = ""
    #: 字幕字号 = min(宽, 高) * 该比例（0.05 ≈ 1080 宽的竖屏 54px）
    compose_subtitle_font_scale: float = 0.05
    #: 字幕距底部 = min(宽, 高) * 该比例
    compose_subtitle_margin_scale: float = 0.04
    #: 视频模型自带音轨怎么处理：off = 丢弃（人声最干净）；under = 压低垫在人声下面
    #: 口型对齐：把配音延后到「画面里的人开口」的时刻（只用本地计算，不重烧视频）。
    #: 真实测量：台词进 prompt 后模型说的同一句话比我们的配音晚 ~1.2 秒开口
    compose_align_dub_to_mouth: bool = True
    #: 口型对齐的最大延后量（秒）；超过就不对齐（避免把尾音挤出片段）
    compose_mouth_align_max_sec: float = 1.5
    compose_ambient_mode: str = "off"
    #: 环境音相对人声的音量（dB）
    compose_ambient_gain_db: float = -20.0
    #: 保留中间分段文件（排查用）
    compose_keep_segments: bool = False

    # 逐帧口型（可选后处理）：把画面里的嘴按配音逐帧重画
    #: none = 不做；wav2lip-onnx = 内置 Wav2Lip（独立 .venv-lipsync）；
    #: cli = 任意外部命令（LatentSync / MuseTalk 等，走 lipsync_cmd 模板）
    lipsync_engine: str = "none"
    #: dialogue = 只处理「说话人在画面里」的镜头（推荐）；all = 所有有配音的镜头
    lipsync_scope: str = "dialogue"
    #: 跑口型脚本的解释器（内置引擎用；默认指向 tools/lipsync/install.sh 建的独立环境）
    lipsync_python: str = ".venv-lipsync/bin/python"
    #: 模型目录（默认 assets/lipsync，已被 .gitignore 排除）
    lipsync_model_dir: str = "assets/lipsync"
    lipsync_model: str = "wav2lip_gan.onnx"
    lipsync_detector: str = "face_detection_yunet_2023mar.onnx"
    #: cli 引擎的命令模板，必须含 {video} {audio} {out} 三个占位符
    lipsync_cmd: str = ""
    #: 追加给内置引擎的额外参数（空格分隔），例如 --crop-mode face --paste-top 0.55
    lipsync_extra_args: str = ""
    #: 单个镜头的超时（秒）
    lipsync_timeout_sec: float = 900.0
    #: >0 时只对时长不超过它的镜头做逐帧口型（控时间；0 = 不限）
    lipsync_max_sec: float = 0.0
    #: 是否顺带量「配音区间 / 嘴部改写区间」写进报告
    lipsync_measure: bool = True
    lipsync_concurrency: int = 1

    # 成本与预算（单价都可配，第三方价格会变）
    #: 0 = 不限制；单位 USD
    budget_usd: float = 0.0
    budget_warn_ratio: float = 0.8
    llm_input_price_per_mtok: float = 0.30
    llm_output_price_per_mtok: float = 2.50
    #: 真实视频 API 按秒计费（mock = 0），例如可灵约 0.05/秒
    video_price_per_second: float = 0.0
    #: 真实 TTS 按字符计费（edge-tts 免费 = 0）
    tts_price_per_mchar: float = 0.0
    #: 可切换的模型候选（逗号分隔），用于前端下拉
    llm_model_candidates: str = (
        "google/gemini-2.5-flash,google/gemini-2.5-pro,anthropic/claude-3.5-haiku"
    )

    # 角色一致性
    #: 固定 seed（0 = 不指定），真实 provider 用来稳定人物外观
    video_seed: int = 0
    reference_image_max_mb: float = 8.0
    #: 剧集资产：场景/道具有参考图时也一起送进视频模型（角色图永远排在最前）
    video_asset_references: bool = True
    #: 单个镜头最多带几张参考图（角色 + 场景 + 道具合计）
    video_max_reference_images: int = 4
    #: 只认首帧、不收参考图的模型（Kling / Wan）怎么锁角色：`auto` = 每段开拍前用图片模型
    #: 把「定妆照 + 场景图 + 第一个镜头」画成一张开场关键帧当首帧（按张计费）；`off` = 不画
    video_keyframe: str = "auto"

    # 批量生产
    batch_max_items: int = 20

    # Paths
    projects_dir: Path = Path("./projects")
    static_dir: Path | None = None
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = ""
    #: 主 ffmpeg 缺能力（例如没有 libass 烧不了字幕）时，是否自动换用候选里
    #: 能满足要求的那个（imageio-ffmpeg 便携版通常带 libass）
    ffmpeg_auto_upgrade: bool = True
    # 角色图（AI 生成参考图）
    image_provider: str = "openrouter"
    openrouter_image_model: str = "bytedance-seed/seedream-4.5"
    #: 角色图宽高比 / 分辨率档位（留空 = 交给模型默认）
    image_aspect_ratio: str = "1:1"
    image_resolution: str = ""
    image_timeout_sec: float = 180.0
    #: 图片接口失败重试的退避基数（秒）；测试里置 0
    image_retry_backoff_sec: float = 2.0
    #: 模型没报 usage.cost 时用于记账的单价（USD / 张）
    image_price_usd: float = 0.04
    #: 生成角色图时是否带上已有参考图（做「同一角色的另一角度」）
    image_use_existing_reference: bool = False
    #: 追加到定妆照 prompt 末尾的额外约束（**必须英文**，中文会被模型画进画面）。
    #: 角色设定里没写人种时，用 'East Asian cast, Chinese television drama'
    #: 之类统一整组角色的面貌基调
    image_prompt_suffix: str = ""
    #: 名字 → 国家 → 外貌锚点：中文名给中国脸、日文名给日本脸（详见 utils/region.py）
    image_region_auto: bool = True
    #: 名字看不出国籍时的兜底国家（cn/jp/kr/west/india/mideast/africa/latam/sea/other）
    image_region_default: str = "cn"
    #: 强制整片按某个国家来（留空 = 自动；填 cn/jp/kr/west 可压过自动判断）
    image_region_override: str = ""

    # 安全：留空表示不校验（仅建议在本机 127.0.0.1 上跑）；设置后所有 /api 请求需带
    # X-API-Token 头或 ?token= 查询参数
    api_token: str = Field(default="", repr=False)

    @property
    def projects_path(self) -> Path:
        return self.projects_dir.resolve()

    @property
    def static_path(self) -> Path:
        if self.static_dir:
            return self.static_dir.resolve()
        return _REPO_ROOT / "static"

    @property
    def has_llm_key(self) -> bool:
        """只暴露「有没有配」，绝不返回密钥本身。"""
        return bool(self.openrouter_api_key.strip())

    @property
    def bgm_path(self) -> Path:
        return self.bgm_dir.expanduser().resolve()

    @property
    def bgm_engine_mode(self) -> str:
        """配乐引擎，非法值一律当 library（绝不因为配置笔误把配乐静默关掉）。"""
        engine = (self.bgm_engine or "library").strip().lower()
        return engine if engine in {"library", "generate", "cli", "auto", "none"} else "library"

    @property
    def bgm_on(self) -> bool:
        return self.bgm_enabled and self.bgm_engine_mode != "none"

    @property
    def music_reverb_ratio(self) -> float:
        """混响湿量；<0 = 交给色调自己定。"""
        value = float(self.music_reverb)
        return -1.0 if value < 0 else min(1.0, max(0.0, value))

    @property
    def candidate_models(self) -> list[str]:
        models = [m.strip() for m in (self.llm_model_candidates or "").split(",")]
        seen: list[str] = []
        for model in models:
            if model and model not in seen:
                seen.append(model)
        if self.openrouter_model and self.openrouter_model not in seen:
            seen.insert(0, self.openrouter_model)
        return seen

    @property
    def audio_target(self) -> float:
        """配音目标响度（限幅在 -30..-10，别让笔误把配音变成蚊子叫或爆音）。"""
        return min(-10.0, max(-30.0, float(self.audio_target_lufs)))

    @property
    def audio_max_gain_db(self) -> float:
        return min(30.0, max(0.0, float(self.audio_normalize_max_db)))

    @property
    def target_lufs(self) -> float:
        return min(-8.0, max(-30.0, float(self.compose_target_lufs)))

    @property
    def true_peak_db(self) -> float:
        return min(0.0, max(-6.0, float(self.compose_true_peak_db)))

    @property
    def ambient_mode(self) -> str:
        mode = (self.compose_ambient_mode or "off").strip().lower()
        return mode if mode in {"off", "under"} else "off"

    @property
    def subtitle_mode(self) -> str:
        mode = (self.compose_subtitle_mode or "soft").strip().lower()
        return mode if mode in {"soft", "burn", "none"} else "soft"

    @property
    def audio_script_mode(self) -> str:
        """配音文案模式，非法值一律当成 off（绝不让配置笔误偷偷花钱）。"""
        mode = (self.audio_llm_script or "off").strip().lower()
        return mode if mode in {"off", "narration", "polish", "full", "perform"} else "off"

    @property
    def audio_frame_count(self) -> int:
        return min(3, max(1, int(self.audio_llm_frames_per_shot or 1)))

    @property
    def audio_frame_width(self) -> int:
        return min(1600, max(160, int(self.audio_llm_frame_width or 640)))

    @property
    def image_region(self) -> str:
        """归一后的兜底国家（非法值一律当 cn，绝不让笔误把脸谱带偏）。"""
        return region.normalize_region(self.image_region_default) or "cn"

    @property
    def image_region_forced(self) -> str:
        return region.normalize_region(self.image_region_override)

    @property
    def audio_motion_rate(self) -> float:
        return min(12.0, max(2.0, float(self.audio_motion_fps or 6)))

    @property
    def lipsync(self) -> str:
        """逐帧口型引擎，非法值一律当成 none（绝不因为配置笔误偷偷改画面）。"""
        engine = (self.lipsync_engine or "none").strip().lower()
        return engine if engine in {"none", "wav2lip-onnx", "cli"} else "none"

    @property
    def dub_guide_mode(self) -> str:
        """配音参照原声的模式，非法值一律当 off（绝不因为笔误改变配音的位置）。"""
        mode = (self.audio_dub_guide or "off").strip().lower()
        return mode if mode in {"off", "native"} else "off"

    @property
    def lipsync_scope_mode(self) -> str:
        scope = (self.lipsync_scope or "dialogue").strip().lower()
        return scope if scope in {"dialogue", "all"} else "dialogue"

    @property
    def lipsync_enabled(self) -> bool:
        return self.lipsync != "none"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_cache() -> None:
    """测试用：清掉单例，让环境变量重新生效。"""
    global _settings
    _settings = None
