# Infinite Image Browsing for ComfyUI

这是一个面向 **ComfyUI custom_nodes** 的 Infinite Image Browsing 轻量集成版。插件会在 ComfyUI 内挂载 IIB 页面，默认浏览 ComfyUI 的 `output` / `input` 等内部目录，并支持手动添加外部文件夹进行浏览。

> 说明：当前仓库根目录为后端/ComfyUI 插件代码；`plugin/lora-scripts-ui-main` 如存在，按本项目约定称为前端。当前实际构建用的 Vue 前端源码位于 `vue/`。

## 功能概览

- 在 ComfyUI 顶部菜单/路由中打开 IIB。
- 默认加载 ComfyUI 输出目录。
- 浏览图片、视频、音频和文件夹。
- 文件夹封面预览：在外层浏览文件夹时显示内部最多 4 个媒体文件作为封面。
- 图片缩略图缓存。
- 图片全屏预览、Prompt 查看、Prompt 编辑。
- 图像对比：从启动页打开图像对比拖拽面板，将两张图片拖入后进行对比。
- 启动页入口：图像搜索、模糊搜索、Topic Search、批量下载/归档、图像对比、全局设置等。
- 可添加外部文件夹：本插件默认加载 ComfyUI 内部文件夹，也允许输入外部文件夹路径来浏览外部文件夹和文件。
- 设置持久化：ComfyUI lite 模式下的前端设置会保存到 IIB cache。

## 安装

将本仓库放入 ComfyUI 的 `custom_nodes` 目录，例如：

```text
ComfyUI/custom_nodes/infinite-image-browsing
```

安装 Python 依赖：

```bash
pip install -r requirements.txt
```

如果使用 ComfyUI 便携包，请使用 ComfyUI 自带的 Python 执行安装，例如：

```bash
/path/to/ComfyUI/python_embeded/python.exe -m pip install -r requirements.txt
```

Windows 下按你的环境可能类似：

```powershell
D:\Ai paint\ComfyUI-aki-v1.6\python\python.exe -m pip install -r requirements.txt
```

然后重启 ComfyUI。

启动日志中看到类似内容即代表挂载成功：

```text
[Infinite Image Browsing] Mounted at /iib and /infinite_image_browsing
```

## 访问地址

插件会注册两个路径：

```text
http://127.0.0.1:8188/iib
http://127.0.0.1:8188/infinite_image_browsing
```

其中 `/infinite_image_browsing` 是兼容旧前端构建的 legacy 路径。

## 使用说明

### 浏览 ComfyUI 内部文件夹

打开 IIB 后，默认会显示 ComfyUI 输出文件夹。你可以用 Walk / Fixed / Normal 等模式浏览图片和子文件夹。

### 添加外部文件夹

在启动页点击添加，输入外部文件夹路径即可。添加后的外部路径会保存到：

```text
iib_cache/comfyui_lite/extra_paths.json
```

之后重启 ComfyUI 仍会保留。

### 文件夹封面预览

当外层列表中显示文件夹时，前端会请求：

```text
/infinite_image_browsing/batch_top_4_media_info
```

后端会扫描该文件夹中的媒体文件，并返回最多 4 个图片/视频作为文件夹封面。

### Prompt 编辑

图片预览页中的“编辑提示词”会打开 Prompt 编辑弹窗。ComfyUI lite 模式不会直接修改原图文件，而是将编辑后的提示词保存到 IIB cache 的 sidecar 文件中。再次读取 `/image_geninfo` 时会优先返回编辑后的内容。

这能避免破坏原始生成图片。注意：如果你移动或重命名图片，之前保存的 sidecar Prompt 可能不会自动跟随。

### 图像对比

启动页点击“图像对比”会打开拖拽面板。将两张图片拖入左右区域后，可以在抽屉中对比，也可以打开为新标签页。

## ComfyUI lite 后端说明

ComfyUI 集成版使用 `scripts/iib/comfyui_api.py` 提供轻量后端。它不是完整独立 IIB 服务，主要目标是让 IIB 在 ComfyUI 内直接浏览文件。

已实现或兼容的接口包括：

- `/files`
- `/batch_get_files_info`
- `/batch_top_4_media_info`
- `/image-thumbnail`
- `/img/{filename}`
- `/file`
- `/stream_video`
- `/image_geninfo`
- `/image_geninfo_batch`
- `/update_exif`
- `/image_exif`
- `/check_path_exists`
- `/open_with_default_app`
- `/open_folder`
- `/send_img_path`
- `/gen_info_completed`
- `/app_fe_setting`
- `/db/basic_info`
- `/db/update_image_data`
- `/db/rebuild_index`
- `/db/extra_paths`
- `/db/search_by_substr`
- `/db/match_images_by_tags`
- `/db/get_image_tags`

其中部分 DB/索引/标签接口是兼容实现或空结果实现，用于避免旧前端功能在 ComfyUI lite 模式下直接 404。

## 当前限制

- ComfyUI lite 模式没有完整 IIB 数据库索引能力。
- 图像搜索、标签搜索、Topic Search 等依赖数据库/向量索引的功能，目前以兼容页面和避免报错为主，搜索结果可能为空。
- 视频封面设置接口当前是兼容 no-op。
- Prompt 编辑默认保存为 sidecar 覆盖值，不直接写入原图。

## 开发

### 后端校验

```bash
python -m py_compile __init__.py scripts/iib/comfyui_api.py scripts/iib/comfyui_asgi.py scripts/iib/tool.py scripts/iib/parsers/comfyui.py
```

### 前端构建

```bash
cd vue
npm install
npx vue-tsc --noEmit --pretty false
npm run build
```

构建产物位于：

```text
vue/dist
```

ComfyUI 插件入口：

```text
__init__.py
```

ComfyUI Web 扩展目录：

```text
web/comfyui
```

## 目录结构

```text
.
├── __init__.py                    # ComfyUI custom node/plugin 入口
├── requirements.txt               # Python 依赖
├── scripts/iib/
│   ├── comfyui_api.py             # ComfyUI lite FastAPI 后端
│   ├── comfyui_asgi.py            # aiohttp <-> ASGI 桥接
│   ├── tool.py                    # 媒体识别、EXIF/Prompt 工具
│   └── parsers/                   # 图片生成信息解析器
├── vue/                           # Vue 前端源码
│   └── dist/                      # 前端构建产物
└── web/comfyui/                   # ComfyUI 前端扩展脚本
```

## 排错

### 页面报 `Could not find window.__TAURI_METADATA__`

这是前端在浏览器环境运行时的提示，不是 Tauri 窗口时可以忽略。

### 接口 404

请确认：

1. 已重启 ComfyUI。
2. 浏览器已 `Ctrl + F5` 强刷。
3. 日志中存在：

```text
[Infinite Image Browsing] Mounted at /iib and /infinite_image_browsing
```

如果仍出现具体接口 404，请复制浏览器控制台中完整的请求路径和 ComfyUI 日志。

### 依赖缺失

确认是在 ComfyUI 使用的 Python 环境中安装：

```bash
python -m pip install -r requirements.txt
```

而不是系统另一个 Python 环境。

## 许可证

见 `LICENSE`。
