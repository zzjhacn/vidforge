# vidforge — 模板化口播视频生成引擎

固定卡片图 + 固定格式文案 → 自动合成口播视频（配音 + 逐句字幕 + 画面动效）。

**场景无关内核 + 场景 Profile**：换一份 YAML 就能换个主题（跑步、读书笔记、股票日报……），内核代码零改动。

游泳数据视频是第一个内置场景，也是最完整的示例。

---

## 它解决什么问题

如果你有一类**周期性、格式固定**的内容要发视频——比如每次游完泳发一条数据复盘——那么每次要做的事其实是一样的：把数据卡片配上一句句解说，加字幕，导出。

vidforge 把这件事变成一条命令。你只需要提供：

1. 几张卡片图（自己做的、App 导出的、脚本生成的都行）
2. 一段口播文案

剩下的——切句、配音、算时长、定切换点、烧字幕、合成——全自动。

**它不做什么**：不生成图片，不调用 LLM 写文案。这两件事留给上游（比如让 LLM 解析运动 App 截图并渲染卡片）。vidforge 是一个**确定性的渲染流水线**：同样的输入必然得到同样的输出，不依赖任何模型。

---

## 特性

- **音频驱动的时间轴** —— 画面停留多久由配音实际时长决定，切换点是「测」出来的，不是拍脑袋设的秒数
- **锚点绑定** —— 用文案里的锚点句把内容绑到不同卡片，开场白写长写短都不影响后面的对位
- **长图友好** —— 超出画布的长图自动 `contain` 完整显示，模糊背景填充两侧，零信息损失
- **字幕自己画** —— 不依赖 ffmpeg 的 `drawtext`/`subtitles` 滤镜（很多发行版没编译），用 Pillow 渲染，中文断行和描边完全可控
- **TTS 方案可切换** —— 默认 Edge TTS（免费，非官方接口，本地即可跑）；也可切到 OpenAI 兼容端点 / 阿里云百炼等云端方案，纯配置按名字切换，内核零改动（见 `configs/swim.yaml` 注释示例）
- **命令行 + 可视化双入口** —— 习惯终端就用命令行，不想记参数就开 Web 操作页面（**零第三方 Web 框架**，纯标准库实现）

---

## 快速开始

### 环境要求

| 依赖               | 说明                                                  |
| ---------------- | --------------------------------------------------- |
| Python           | ≥ 3.10                                              |
| ffmpeg / ffprobe | 必须在 PATH 中，或位于 `/opt/homebrew/bin`、`/usr/local/bin` |
| 系统字体             | 任意中文字体（自动探测，见[故障排查](#字幕中文显示为方块)）                    |

> ⚠️ 你的 ffmpeg **不需要** 编译 `libass` 或 `libfreetype`——字幕不走这两个滤镜。这也是本项目的设计选择之一。

### 安装

```bash
git clone https://github.com/<your-account>/vidforge.git
cd vidforge

# 方式一：uv（推荐）
uv sync

# 方式二：pip
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # 或 pip install -e . 装成命令行工具
```

依赖只有三个：`edge-tts`、`PyYAML`、`Pillow`。

### 跑通示例（约 2 分钟）

仓库自带一份游泳场景的完整示例，直接可跑：

```bash
# 1. 先看时间轴预览，不合成（不产出文件，几秒钟）
python -m vidforge -c configs/swim.yaml --dry-run

# 2. 确认无误后出片 → output/swim.mp4
python -m vidforge -c configs/swim.yaml
```

装成命令行工具后更简洁：

```bash
vidforge -c configs/swim.yaml --dry-run
vidforge -c configs/swim.yaml
```

示例产出：约 28 秒 / 1080×1920 竖屏 / 30fps / H.264 + AAC。

或者开**操作页面**，全程点选、不用记参数（详见 [Web 操作页面](#web-操作页面)）：

```bash
vidforge-web                    # 然后浏览器打开 http://127.0.0.1:8765
```

> **目录约定**：配置文件必须放在项目根的 `configs/` 子目录下。中间产物和成片的路径由配置文件位置推导（`<项目根>/work/`、`output/`），放到别处会写到错误的目录。
> 用 Web 页面时没有这个约束——它会在 `web_work/<任务ID>/` 下自动建好完整目录结构。

---

## 工作原理

```
文案 (script.txt)
   │
   ├─ 切句：按句末标点切分，超长句按逗号二次切分（默认 ≤26 字）
   │
   ├─ 绑定：按锚点句把句子分组，每组对应一张卡片
   │
   ▼
逐句 TTS 合成  ──►  ffprobe 实测每段音频时长
   │
   ▼
时间轴计算：组时长 = Σ句时长 + 句间静音 + 尾部留白
           组时长 = max(组时长, 5.5s)      ← 防短句闪切
   │
   ▼
片段渲染（cover + Ken Burns / contain + 模糊背景）
   │
   ▼
拼接 → Pillow 字幕逐句 overlay → 音画合流
   │
   ▼
output/<name>.mp4
```

**三条核心设计**，都是踩过坑才定下来的：

1. **时长必须实测，不能估算。** 按「字数 × 语速」估算误差可达 ±15%——因为 `35分36秒` 要念成「三十五分三十六秒」，字数和发音时长严重不成比例。误差足以造成画面切了话还没说完。
2. **句间要插静音。** 分段合成的音频直接拼接，听起来会喘不上气。默认 200ms。
3. **最小停留保护。** 某次只写一句短句（约 1.6 秒）时，画面不会闪一下就切，而是补足到 5.5 秒。

---

## 输入约定

### 1. 卡片图

放在 `assets/` 下，PNG，**建议宽度统一 1080**。

- 与画布同尺寸（1080×1920）→ 用 `fit: cover` 满屏
- 长图（比如 1080×2200）→ 用 `fit: contain`，完整显示不裁切，两侧用模糊放大的自身填充

> **素材需自备。** 仓库里的 `swim_card1.png` / `swim_card2.png` 是作者一次真实游泳的数据卡片，仅供演示效果（含配速柱状图、心率曲线等）。  
> **请替换为你自己的卡片图**，样式随意，竖版即可。本工具不生成图片。

### 2. 口播文案

放在 `assets/script.txt`，UTF-8 纯文本。

**⚠️ 关键：文案要「为朗读而写」，不是「为阅读而写」。** TTS 念不出的符号必须提前转成中文读法：

| ❌ 别这么写       | ✅ 这么写             |
| ------------ | ----------------- |
| `2′22″/100m` | `2分22秒每100米`      |
| `625kcal`    | `625千卡`           |
| `129BPM`     | `129` 或 `每分钟129次` |
| `35:36`      | `35分36秒`          |

文案里的**锚点句**用来把内容绑到不同卡片，示例用「数据复盘来了」：

```
[自由陈述 1-2 句]。数据复盘来了：[固定格式的数据句，每次只换数字]。
                  └── 锚点，可在 YAML 的 script.split_markers 更换
```

锚点之前绑第一张卡，之后绑第二张。这样即使开场白每次长短不同，数据部分也能稳定落到第二张卡片上。

> 找不到锚点**不会报错**，而是静默降级为「全部内容绑到第一张卡」。如果你改了措辞，记得同步改 YAML 里的 `split_markers`。

---

## 配置参考

场景配置就是一份 YAML（`configs/swim.yaml` 带完整注释）：

```yaml
name: swim

canvas:
  width: 1080
  height: 1920
  fps: 30

tts:
  scheme: edge               # TTS 方案名：edge（默认）| openai | bailian
  voice: zh-CN-YunxiNeural    # 云希，年轻男声
  rate: "+10%"                # 语速（edge / openai 支持；bailian 忽略）
  silence_between_ms: 200     # 句间静音
  tail_padding_ms: 400        # 每组尾部留白
  # openai: { endpoint: "http://host/v1/audio/speech", key: "", model: "local", format: wav, concurrency: 4 }
  # bailian: { endpoint: "https://{ws}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer", key: "<KEY>", model: "qwen-audio-3.0-tts-flash", sample_rate: 24000 }
  # 不同方案有各自独立的音色列表，Web 操作台会「先选方案、再选音色」

timeline:
  min_group_duration: 5.5     # 单张图最短停留（秒），防短句闪切

script:
  source: assets/script.txt
  max_sentence_chars: 26      # 超长句按逗号二次切分的阈值
  split_markers:
    - "数据复盘来了"           # 绑定锚点

assets:
  - name: record
    file: assets/swim_card1.png
    fit: cover                 # 满屏
    motion: { type: kenburns, from: 1.0, to: 1.06 }

  - name: analysis
    file: assets/swim_card2.png
    fit: contain               # 长图完整显示
    background: blur
    background_blur: 24
    motion: { type: static }   # 前景静止，保证图表可读
    background_motion: { type: kenburns, from: 1.0, to: 1.08 }

bind:
  - { asset: record,   range: "through:0" }   # 到锚点句为止（含）
  - { asset: analysis, range: "after:0" }     # 锚点句之后

subtitle:
  enabled: true
  size: 58
  bottom_margin: 240          # 距底边距离，避开卡片数据区
  box: true                   # 圆角半透明底框
  box_alpha: 96

output:
  file: output/swim.mp4
  video_codec: libx264
  crf: 20
  audio_codec: aac
  audio_bitrate: 192k
```

### 绑定规则（`bind.range`）

| 写法          | 含义                      |
| ----------- | ----------------------- |
| `before:N`  | 第 N 个锚点之前的句子            |
| `through:N` | 到第 N 个锚点所在句为止（**含**锚点句） |
| `after:N`   | 第 N 个锚点所在句之后（不含）        |
| `from:N`    | 第 N 个锚点（含）之后的所有句子       |
| `all`       | 全部句子                    |

`through:N` 与 `after:N` 互补成对，不重不漏。

### 素材参数（`assets`）

| 字段                  | 说明                                                        |
| ------------------- | --------------------------------------------------------- |
| `fit`               | `cover` 满屏裁切填充 / `contain` 完整显示                           |
| `motion`            | 前景动效：`{type: kenburns, from, to}` 缓慢推近，或 `{type: static}` |
| `background`        | `contain` 模式下的背景填充方式，目前支持 `blur`                          |
| `background_motion` | 背景可独立做动效，与前景解耦                                            |

### 字幕样式（`subtitle`）

常用字段：`size`、`color`、`stroke_width`、`stroke_color`、`bottom_margin`、`box`、`box_alpha`、`box_padding`、`box_radius`。

字体默认**自动探测**系统中文字体，无需配置。要固定的话：

```yaml
subtitle:
  font: "/System/Library/Fonts/Hiragino Sans GB.ttc"   # macOS
  # font: "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"  # Linux
```

路径不存在时会自动回退到探测结果并告警，不会崩。

---

## 自定义新场景

复制 `configs/swim.yaml`，改四件事即可，**内核代码零改动**：

1. `script.source` —— 你的文案文件
2. `script.split_markers` —— 你的锚点句
3. `assets` —— 你的卡片图与 `fit` 方式
4. `subtitle.bottom_margin` —— 按卡片内容调整，别让字幕盖住关键数字

然后 `vidforge -c configs/<你的场景>.yaml`。

---

## 命令行

```
usage: vidforge [-h] --config CONFIG [--dry-run]

选项：
  --config, -c   场景配置 YAML 路径（必需）
  --dry-run      只打印切句、绑定与时间轴预览，不合成视频
```

`--dry-run` 适合调文案：确认切句符合预期、切换点落在语义边界上，再跑全量。

---

## Web 操作页面

不想记参数就用它。拖拽上传卡片图、填文案、调配置、看实时进度、预览下载，全流程可视化。

```bash
vidforge-web                       # 或 python -m vidforge.web
vidforge-web --port 9000           # 换端口
```

浏览器打开 `http://127.0.0.1:8765`（默认只监听本机）。

**零第三方 Web 框架**——用 Python 标准库 `http.server` 实现，不装任何新包，命令行用法完全不受影响。

### 页面能做什么

| 区域 | 说明 |
|---|---|
| 卡片图 | 点击或拖拽上传，支持多张，**上传顺序即播放顺序**，可单独删除重排 |
| 口播文案 | 直接编辑，底部有「为朗读而写」的对照提示 |
| 生成配置 | 音色、语速、画布尺寸、图片适配方式（contain / cover）、锚点句 |
| 字幕 | 开关、字号、距底边距离、最大句长 |
| 进度 | 实时百分比 + 逐条日志（与命令行输出一致） |
| 结果 | 页面内直接播放预览，一键下载成片 |

### 锚点句怎么用

**N 张图需要 N-1 个锚点句**（多个用逗号分隔）。锚点之前的内容归前一张图：

```
1 张图：不需要锚点，全部内容归它
2 张图：「数据复盘来了」→ 前段归图1，后段归图2
3 张图：「开场说完。数据复盘来了」→ 分成三段
```

页面会自动按这个规则生成绑定，不用手写 YAML。

### 技术实现

- 上传的文件落到 `web_work/<任务ID>/assets/`，配置自动生成到同目录的 `configs/scene.yaml`
- 生成在后台线程跑，前端轮询 `/api/task/<id>` 拿进度
- 视频预览支持 HTTP Range 请求，浏览器可以正常拖动进度条
- 编排逻辑与命令行**同一个函数**（`pipeline.build`），不存在两套实现

---

## 故障排查

### edge-tts 随机失败：`NoAudioReceived`

**现象**：同一份代码连续跑两次，一次成功一次失败。

**原因**：Edge TTS 是微软**未公开**的接口（请求里的 `TrustedClientToken` 是从 Edge 浏览器提取的），有频率风控。与你的参数无关。

**对策**（已内置）：串行合成 + 4 次指数退避重试。若失败率持续升高，降低调用频率，或换用其他 TTS。

> 另外注意版本：`edge-tts < 7.2.4` 会因微软 2025-12 的接口变更直接 403。请确保 `edge-tts >= 7.2.8`。

### 字幕中文显示为方块 / 找不到字体

字体是自动探测的，会依次尝试 macOS / Linux / Windows 的常见中文字体路径。都没找到时会告警并回退到 PIL 默认字体（中文变方块，但不崩）。

Linux 上装字体即可：

```bash
apt install fonts-noto-cjk      # Debian/Ubuntu
yum install google-noto-sans-cjk-fonts   # CentOS/RHEL
```

或在 YAML 里显式指定 `subtitle.font`。

### 找不到 ffmpeg

```bash
ffmpeg -version   # 先确认是否安装
```

macOS：`brew install ffmpeg`　Linux：`apt install ffmpeg`

代码会依次尝试 `/opt/homebrew/bin`、`/usr/local/bin`、`/opt/local/bin`，最后回落到 PATH。

### 字幕盖住了卡片上的关键内容

调 `subtitle.bottom_margin`（距底边像素）。改完用 `--dry-run` 看不到效果，需要实际出片后抽帧确认：

```bash
ffmpeg -ss 8 -i output/swim.mp4 -frames:v 1 /tmp/check.png
```

### 调试时误判运行成功

`cmd 2>&1 | tail -20` 之后取 `$?` 拿到的是 **tail 的退出码**（zsh 默认不开 `pipefail`）。调试时重定向到文件再查看：

```bash
vidforge -c configs/swim.yaml > /tmp/run.log 2>&1; echo "exit=$?"
```

---

## 平台支持

| 平台      | 状态                                  |
| ------- | ----------------------------------- |
| macOS   | ✅ 完整验证（示例成片在此产出）                    |
| Linux   | ⚠️ 理论可用，字体与 ffmpeg 已做探测，但**未经实际验证** |
| Windows | ⚠️ 未测试                              |

欢迎在 Linux / Windows 上试用并提交 Issue 反馈。

---

## 已知限制

- **无增量渲染**：每次全量重跑（示例约 100 秒）。改一个字幕位置也要重新渲染全部片段。
- **字幕必然遮挡部分画面**：竖屏画布上卡片内容通常很满。当前示例配置已逐帧核对无信息冲突，但**换了卡片样式要重新检查**。
- **长句切分较机械**：按逗号贪心合并到阈值字数，可能在「最快 / 配速1分58秒」这类位置断开。可接受，但未做语义感知。
- **配音依赖 Edge TTS**：免费但非官方接口，有风控。长期或商用建议换成火山豆包、腾讯云等正式服务（改 `tts.py` 一处即可）。

---

## 路线图

按优先级排列，欢迎 PR：

- [x] 编程接口 `build()`，返回结构化时间轴（已完成：`pipeline.build`，命令行与 Web 共用）
- [x] TTS 供应商抽象层：按名字注册的 scheme（edge / openai / bailian），纯配置切换，见 `configs/swim.yaml` 注释示例与 `vidforge/tts.py` 的 `REGISTRY`
- [ ] 历史数据存储 + 对比洞察（需接入 LLM，与断言校验配套）
- [ ] 数据自洽性校验（如「平均配速 × 距离 ≈ 总时长」，防上游数字抄错）
- [ ] 增量渲染，复用未变动的片段
- [ ] Web：任务持久化与过期清理（当前任务状态在内存，重启即失）
- [ ] Web：场景配置的保存/加载（复用已生成的 profile）

---

## 项目结构

```
vidforge/           内核包（场景无关）
  cli.py              命令行入口
  pipeline.py         生成编排（命令行与 Web 共用）
  profile.py          Profile 加载与默认值
  script.py           切句 + 绑定规则
  tts.py              逐句合成 + 重试 + 实测时长
  timeline.py         音频驱动的画面停留区间
  subtitle.py         Pillow 字幕渲染（含字体探测）
  compose.py          片段渲染 / 拼接 / 字幕烧录 / 合流
  util.py             ffmpeg 定位、时长探测、音轨构建
  web/                操作页面（零第三方依赖，纯标准库）
    server.py           HTTP 服务、任务管理、multipart 解析
    static/index.html   单页 UI
configs/            场景配置（YAML）
assets/             输入素材（卡片图 + 文案）
work/               中间产物（已 gitignore）
output/             成片（已 gitignore）
web_work/           Web 页面上传的任务目录（已 gitignore）
```

---

## 贡献

Issue 和 PR 都欢迎。几个方向特别需要帮助：

- Linux / Windows 上的实际验证与路径适配
- 更多场景 Profile 示例（跑步、读书、财报……）
- 长句切分的语义感知

提交前请确保 `vidforge -c configs/swim.yaml --dry-run` 能正常跑通。

---

## License

见 [LICENSE](LICENSE)。
