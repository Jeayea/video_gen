# BGM 曲库

把背景音乐放在这个目录（`BGM_DIR`，默认 `./assets/bgm`），生成配音时按镜头情绪自动选曲。

## 目录约定

- 音频文件：`.mp3` / `.m4a` / `.wav` / `.flac` / `.ogg` / `.aac` / `.opus`
- 可选元数据：本目录下的 `bgm.json`（文件名固定）

## bgm.json 格式

```json
{
  "tracks": [
    {
      "id": "tense_01",
      "title": "暗涌",
      "file": "tense_01.mp3",
      "moods": ["紧张", "悬疑", "压抑", "冲突"],
      "duration_sec": 45,
      "gain_db": -22,
      "license": "CC0"
    }
  ]
}
```

- `moods` 决定自动匹配：会拿分镜里出现过的 `mood` 去比对（支持互相包含，`紧张` 能匹配 `紧张的`）
- `gain_db` 是该曲的相对音量（默认取 `BGM_GAIN_DB`，一般 `-20` 左右，别盖住人声）
- 没写进 `bgm.json` 的音频文件也会被自动收录：文件名按 `_ - 空格` 切出情绪标签
  （`tense_01.mp3` → id=`tense_01`，moods=`["tense"]`）
- 曲库为空不是错误：**没有 BGM 就出无声底的片子**，不会报错

## 不想找素材？

`BGM_ENGINE=generate` 可以让**本机按这条片子的分镜现写一首**，不需要曲库、不联网、$0：
段落边界对齐镜头切换、画面动作最猛处给重音、有人说话的段落去鼓并让旋律避开说话窗口，
合成后过卷积混响与响度归一。详见 [README.md](../../README.md) 的「配乐」一节。

曲库模式（本文件描述的这套）适合「我手上已经有版权音乐」的情况，
两者可以用 `BGM_ENGINE=library|generate|auto` 切换（`auto` = 有歌就用歌，没歌就现场写）。

## 演示曲目

没有素材时可以先生成几首免版权的合成垫音（正弦波和弦，仅用于跑通链路）：

```bash
python -m videogen.tools.demo_bgm            # 生成 4 首到 ./assets/bgm
python -m videogen.tools.demo_bgm --force    # 覆盖
```

生成的文件与 `bgm.json` 都在本目录；`.gitignore` 忽略了 `*.mp3`，所以演示音频不会进版本库。
正式使用请替换成有明确授权的音乐。
