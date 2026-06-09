from __future__ import annotations

from datetime import datetime, timedelta
import asyncio
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Dict, Iterable, List, Optional
import urllib.parse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageOps
from pydantic import BaseModel

from scripts.iib.fastapi_video import range_requests_response
from scripts.iib.logger import logger
from scripts.iib.parsers.comfyui import ComfyUIParser
from scripts.iib.tool import (
    get_cache_dir,
    get_created_date_by_stat,
    get_formatted_date,
    get_video_type,
    human_readable_size,
    is_audio_file,
    is_image_file,
    is_media_file,
    is_video_file,
    parse_generation_parameters,
)

try:
    import pillow_avif  # noqa: F401
except Exception as exc:  # pragma: no cover - optional codec
    logger.debug("pillow_avif is not available: %s", exc)


_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE = "/infinite_image_browsing"
index_html_path = _PACKAGE_ROOT / "vue" / "dist" / "index.html"


class PathsReq(BaseModel):
    paths: List[str]


class PathReq(BaseModel):
    path: str


class UpdateExifReq(BaseModel):
    path: str
    exif: str


class ExtraPathReq(BaseModel):
    path: str
    types: List[str] = []
    alias: Optional[str] = None


class FilePathsReq(BaseModel):
    file_paths: List[str]


class FileTransferReq(FilePathsReq):
    dest: str
    create_dest_folder: Optional[bool] = False
    continue_on_error: Optional[bool] = False


class MkdirsReq(BaseModel):
    dest_folder: str


class VideoCoverReq(BaseModel):
    path: str
    base64_img: str
    updated_time: str = ""


class ComfyUILiteConfig:
    def __init__(
        self,
        output_dir: str | os.PathLike[str],
        input_dir: str | os.PathLike[str] | None = None,
        base: str = "/iib",
    ) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.input_dir = Path(input_dir).resolve() if input_dir else None
        self.base = base if base.startswith("/") else f"/{base}"
        self.allowed_roots = [self.output_dir]
        if self.input_dir and self.input_dir.exists():
            self.allowed_roots.append(self.input_dir)


class _FolderCacheEntry:
    def __init__(self, mtime_ns: int, files: List[Dict[str, Any]]) -> None:
        self.mtime_ns = mtime_ns
        self.files = files


class ComfyUILiteApi:
    """Small read-only API surface for browsing ComfyUI output inside ComfyUI.

    This intentionally avoids DB/tag/search/desktop-wide features from the full IIB server.
    """

    def __init__(self, config: ComfyUILiteConfig) -> None:
        self.config = config
        self.cache_base_dir = get_cache_dir()
        self.extra_paths = self._read_extra_paths()
        self.folder_cache: Dict[str, _FolderCacheEntry] = {}
        self.search_index: List[Dict[str, Any]] = []
        self.tag_index: Dict[str, Dict[str, Any]] = {}
        self.search_index_roots_key = ""

    def create_app(self) -> FastAPI:
        app = FastAPI()
        self.mount(app)
        return app

    def mount(self, app: FastAPI) -> None:
        base = self.config.base

        @app.get("/")
        def root_index():
            return self._index_response()

        @app.get(base)
        def index():
            return self._index_response()

        @app.get("/fe-static/{file_path:path}")
        async def serve_static_file(file_path: str):
            return self._serve_static_file(file_path)

        @app.get(f"{base}/fe-static/{{file_path:path}}")
        async def serve_static_file_with_base(file_path: str):
            return self._serve_static_file(file_path)

        @app.get(f"{base}/hello")
        async def hello():
            return "hello"

        @app.get(f"{base}/global_setting")
        async def global_setting():
            output = str(self.config.output_dir)
            return {
                "global_setting": self._read_app_fe_setting("global"),
                "cwd": output,
                "is_win": os.name == "nt",
                "home": "",
                "sd_cwd": output,
                "all_custom_tags": self._get_all_custom_tags(),
                "extra_paths": self._global_extra_paths(),
                "enable_access_control": False,
                "launch_mode": "comfyui",
                "export_fe_fn": True,
                "app_fe_setting": self._read_all_app_fe_settings(),
                "is_readonly": False,
            }

        @app.get(f"{base}/version")
        async def version():
            return {"hash": "comfyui-lite", "tag": "comfyui-lite"}

        @app.get(f"{base}/files")
        async def files(folder_path: str):
            folder = self._resolve_trusted_path(folder_path, allow_root_parent=True)
            if not folder.exists() or not folder.is_dir():
                raise HTTPException(status_code=404, detail="Folder does not exist")
            return {"files": await asyncio.to_thread(self._list_folder, folder)}

        @app.post(f"{base}/batch_get_files_info")
        async def batch_get_files_info(req: PathsReq):
            res: Dict[str, Optional[Dict[str, Any]]] = {}
            for item in req.paths:
                try:
                    path = self._resolve_trusted_path(item, allow_root_parent=True)
                    res[item] = await asyncio.to_thread(self._file_info, path) if path.exists() else None
                except HTTPException:
                    res[item] = None
            return res

        @app.get(f"{base}/image-thumbnail")
        async def image_thumbnail(path: str, t: str, size: str = "256x256"):
            target = self._resolve_trusted_path(path)
            if not target.exists() or not target.is_file():
                logger.warning("Thumbnail requested for missing image: %r", str(target))
                raise HTTPException(status_code=404, detail=f"Image does not exist: {target}")
            if not is_image_file(str(target)):
                raise HTTPException(status_code=400, detail="Not an image file")
            return await asyncio.to_thread(self._thumbnail_response, target, t, size)

        @app.get(f"{base}/img/{{filename}}")
        async def get_image(filename: str, path: str, t: str):
            target = self._resolve_trusted_path(path)
            decoded_filename = urllib.parse.unquote(filename)
            if target.name != decoded_filename:
                raise HTTPException(status_code=400, detail="Filename mismatch")
            if not target.exists() or not target.is_file():
                raise HTTPException(status_code=404)
            if not is_image_file(str(target)):
                raise HTTPException(status_code=400, detail="Not an image file")
            media_type, _ = mimetypes.guess_type(str(target))
            return FileResponse(
                str(target),
                media_type=media_type,
                headers=self._long_cache_headers(target.name),
            )

        @app.get(f"{base}/file")
        async def get_file(path: str, t: str, disposition: Optional[str] = None):
            target = self._resolve_trusted_path(path)
            if not target.exists() or not target.is_file():
                raise HTTPException(status_code=404)
            media_type, _ = mimetypes.guess_type(str(target))
            headers = self._long_cache_headers(disposition)
            return FileResponse(str(target), media_type=media_type, headers=headers)

        @app.get(f"{base}/stream_video")
        async def stream_video(path: str, request: Request):
            target = self._resolve_trusted_path(path)
            if not target.exists() or not target.is_file():
                raise HTTPException(status_code=404)
            media_type, _ = mimetypes.guess_type(str(target))
            return range_requests_response(request, file_path=str(target), content_type=media_type)

        @app.get(f"{base}/image_geninfo")
        async def image_geninfo(path: str):
            target = self._resolve_trusted_path(path)
            return self._read_comfyui_geninfo(target)

        @app.get(f"{base}/comfyui_workflow")
        async def comfyui_workflow(path: str):
            target = self._resolve_trusted_path(path)
            workflow = self._read_comfyui_workflow(target)
            if not workflow:
                raise HTTPException(status_code=404, detail="No ComfyUI workflow found in this image")
            return workflow

        @app.post(f"{base}/update_exif")
        async def update_exif(req: UpdateExifReq):
            # The full backend stores edited prompt metadata in IIB's DB. The
            # ComfyUI-lite backend intentionally has no DB, so keep a small
            # sidecar store next to the image cache and let /image_geninfo read
            # it back. This restores the prompt editor UX without mutating the
            # original generated file.
            target = self._resolve_trusted_path(req.path)
            if not target.exists() or not target.is_file() or not is_image_file(str(target)):
                raise HTTPException(status_code=404, detail="Image does not exist")
            self._write_geninfo_override(target, req.exif)
            return {"success": True, "message": "Prompt metadata saved"}

        @app.post(f"{base}/open_with_default_app")
        async def open_with_default_app(req: PathReq):
            target = self._resolve_trusted_path(req.path, allow_root_parent=True)
            if not target.exists():
                raise HTTPException(status_code=404, detail="Path does not exist")
            self._open_path_with_os(target)
            return {"success": True}

        @app.post(f"{base}/open_folder")
        async def open_folder(req: PathReq):
            target = self._resolve_trusted_path(req.path, allow_root_parent=True)
            if not target.exists():
                raise HTTPException(status_code=404, detail="Path does not exist")
            self._open_path_with_os(target if target.is_dir() else target.parent)
            return {"success": True}

        @app.post(f"{base}/send_img_path")
        async def send_img_path(path: str):
            # SD-WebUI uses this endpoint to transfer an image to txt2img/img2img.
            # In ComfyUI mode the actual transfer is done by frontend messages;
            # accepting the request prevents toolbar buttons from failing with 404.
            target = self._resolve_trusted_path(path)
            if not target.exists() or not target.is_file():
                raise HTTPException(status_code=404, detail="Image does not exist")
            return {"success": True}

        @app.get(f"{base}/gen_info_completed")
        async def gen_info_completed():
            return True

        @app.post(f"{base}/image_geninfo_batch")
        async def image_geninfo_batch(req: PathsReq):
            res: Dict[str, str] = {}
            for item in req.paths:
                try:
                    target = self._resolve_trusted_path(item)
                    res[item] = self._read_comfyui_geninfo(target)
                except Exception:
                    res[item] = ""
            return res

        @app.post(f"{base}/db/update_image_data")
        async def update_image_data():
            # Build a lightweight filename/path/prompt index for ComfyUI mode.
            # It does not implement the full IIB tag DB, but it makes the
            # search pages actually useful instead of only returning success.
            await asyncio.to_thread(self._rebuild_search_index)
            return {"success": True, "count": len(self.search_index)}

        @app.post(f"{base}/db/rebuild_index")
        async def rebuild_index():
            await asyncio.to_thread(self._rebuild_search_index, True)
            return {"success": True, "count": len(self.search_index)}

        @app.get(f"{base}/db/extra_paths")
        async def get_extra_paths():
            return self.extra_paths

        @app.post(f"{base}/db/extra_paths")
        async def add_extra_path(req: ExtraPathReq):
            target = self._resolve_path(req.path, allow_any_existing=True)
            if not target.exists() or not target.is_dir():
                raise HTTPException(status_code=404, detail="Folder does not exist")
            self._upsert_extra_path(str(target), req.types or ["scanned-fixed"], req.alias)
            return {"success": True}

        @app.delete(f"{base}/db/extra_paths")
        async def remove_extra_path(req: ExtraPathReq):
            self._remove_extra_path(req.path, req.types)
            return {"success": True}

        @app.post(f"{base}/db/alias_extra_path")
        async def alias_extra_path(req: ExtraPathReq):
            self._upsert_extra_path(req.path, req.types or [], req.alias)
            return {"success": True}

        @app.post(f"{base}/db/match_images_by_tags")
        async def match_images_by_tags(request: Request):
            body = await request.json()
            await asyncio.to_thread(self._ensure_search_index)
            return await asyncio.to_thread(self._match_images_by_tags, body)

        @app.post(f"{base}/db/search_by_substr")
        async def search_by_substr(request: Request):
            body = await request.json()
            await asyncio.to_thread(self._ensure_search_index)
            return await asyncio.to_thread(self._search_by_substr, body)

        @app.get(f"{base}/db/expired_dirs")
        async def expired_dirs():
            return {"expired": False, "expired_dirs": []}

        @app.get(f"{base}/db/random_images")
        async def random_images():
            return []

        @app.get(f"{base}/db/img_selected_custom_tag")
        async def img_selected_custom_tag(path: str):
            return self._get_selected_custom_tags(path)

        @app.post(f"{base}/db/add_custom_tag")
        async def add_custom_tag(request: Request):
            body = await request.json()
            tag_name = str(body.get("tag_name") or "").strip()
            if not tag_name:
                raise HTTPException(status_code=400, detail="Invalid tag name")
            return self._add_custom_tag(tag_name)

        @app.post(f"{base}/db/update_tag")
        async def update_tag(request: Request):
            body = await request.json()
            return self._update_custom_tag(body)

        @app.post(f"{base}/db/remove_custom_tag")
        async def remove_custom_tag(request: Request):
            body = await request.json()
            self._remove_custom_tag(body.get("tag_id"))
            return {"success": True}

        @app.post(f"{base}/db/toggle_custom_tag_to_img")
        async def toggle_custom_tag_to_img(request: Request):
            body = await request.json()
            return self._toggle_custom_tag_to_img(body.get("tag_id"), body.get("img_path"))

        @app.post(f"{base}/db/batch_update_image_tag")
        async def batch_update_image_tag(request: Request):
            body = await request.json()
            return self._batch_update_image_tag(body)

        @app.post(f"{base}/db/get_image_tags")
        async def get_image_tags(req: PathsReq):
            return {path: self._get_selected_custom_tags(path) for path in req.paths}

        @app.post(f"{base}/batch_top_4_media_info")
        async def batch_top_4_media_info(req: PathsReq):
            res: Dict[str, List[Dict[str, Any]]] = {}
            for item in req.paths:
                try:
                    folder = self._resolve_trusted_path(item)
                    if not folder.exists() or not folder.is_dir():
                        res[item] = []
                        continue
                    res[item] = await asyncio.to_thread(self._top_media_info, folder)
                except Exception as exc:
                    logger.debug("Failed to read directory cover for %s: %s", item, exc)
                    res[item] = []
            return res

        @app.get(f"{base}/db/basic_info")
        async def get_db_basic_info():
            await asyncio.to_thread(self._ensure_search_index)
            tags = sorted(
                [*self.tag_index.values(), *self._get_all_custom_tags()],
                key=lambda item: (-item.get("count", 0), item.get("type", ""), item.get("name", "")),
            )
            return {"img_count": len(self.search_index), "tags": tags, "expired": False, "expired_dirs": []}

        @app.get(f"{base}/image_exif")
        async def image_exif(path: str):
            target = self._resolve_trusted_path(path)
            if not target.exists() or not target.is_file() or not is_image_file(str(target)):
                return {}
            try:
                return await asyncio.to_thread(self._image_info_without_exif, target)
            except Exception as exc:
                logger.error("Failed to get exif for %s: %s", target, exc)
                return {}

        @app.post(f"{base}/check_path_exists")
        async def check_path_exists(req: PathsReq):
            res: Dict[str, bool] = {}
            for item in req.paths:
                try:
                    res[item] = self._resolve_path(item, allow_any_existing=True).exists()
                except HTTPException:
                    res[item] = False
            return res

        @app.post(f"{base}/mkdirs")
        async def mkdirs(req: MkdirsReq):
            target = self._resolve_path(req.dest_folder, allow_any_existing=True)
            target.mkdir(parents=True, exist_ok=True)
            return {"success": True}

        @app.post(f"{base}/copy_files")
        async def copy_files(req: FileTransferReq):
            return {"files": self._copy_or_move_files(req.file_paths, req.dest, move=False, create_dest_folder=bool(req.create_dest_folder))}

        @app.post(f"{base}/move_files")
        async def move_files(req: FileTransferReq):
            return {"files": self._copy_or_move_files(req.file_paths, req.dest, move=True, create_dest_folder=bool(req.create_dest_folder))}

        @app.post(f"{base}/delete_files")
        async def delete_files(req: FilePathsReq):
            for item in req.file_paths:
                target = self._resolve_path(item, allow_any_existing=True)
                if target.exists() and target.is_file():
                    target.unlink()
            return {"ok": True}

        @app.post(f"{base}/set_target_frame_as_video_cover")
        async def set_target_frame_as_video_cover(req: VideoCoverReq):
            # Video cover caching is optional in ComfyUI-lite; accepting this
            # endpoint prevents preview toolbar actions from failing with 404.
            return {"success": True}

        @app.post(f"{base}/app_fe_setting")
        async def app_fe_setting(request: Request):
            body = await request.json()
            name = str(body.get("name", "")).strip()
            value = body.get("value", "{}")
            if not name:
                raise HTTPException(status_code=400, detail="name is required")
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            self._write_app_fe_setting(name, value)
            return {"success": True}

        @app.delete(f"{base}/app_fe_setting")
        async def remove_app_fe_setting(request: Request):
            try:
                body = await request.json()
            except Exception:
                body = {}
            name = str(body.get("name", "")).strip()
            if name:
                self._delete_app_fe_setting(name)
            return {"success": True}

    def _index_response(self) -> Response:
        with open(index_html_path, "r", encoding="utf-8") as file:
            content = file.read().replace(DEFAULT_BASE, self.config.base)
        return Response(content=content, media_type="text/html")

    def _serve_static_file(self, file_path: str) -> FileResponse:
        static_dir = index_html_path.parent
        target = (static_dir / file_path).resolve()
        try:
            target.relative_to(static_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(str(target))

    def _resolve_trusted_path(self, raw_path: str, allow_root_parent: bool = False) -> Path:
        return self._resolve_path(raw_path, allow_root_parent=allow_root_parent, allow_any_existing=True)

    def _resolve_path(self, raw_path: str, allow_root_parent: bool = False, allow_any_existing: bool = False) -> Path:
        if raw_path in ("", "/"):
            return self.config.output_dir
        target = Path(raw_path).expanduser().resolve()
        if allow_any_existing:
            return target
        roots: Iterable[Path] = self.config.allowed_roots
        for root in roots:
            try:
                target.relative_to(root)
                return target
            except ValueError:
                if allow_root_parent:
                    try:
                        root.relative_to(target)
                        return target
                    except ValueError:
                        pass
        raise HTTPException(status_code=403, detail="Path is outside configured directories")

    def _file_info(self, path: Path) -> Dict[str, Any]:
        stat = path.stat()
        date = get_formatted_date(stat.st_mtime)
        created_time = get_created_date_by_stat(stat)
        if path.is_file():
            return {
                "type": "file",
                "date": date,
                "size": human_readable_size(stat.st_size),
                "name": path.name,
                "bytes": stat.st_size,
                "created_time": created_time,
                "fullpath": str(path),
                "is_under_scanned_path": True,
            }
        return {
            "type": "dir",
            "date": date,
            "created_time": created_time,
            "size": "-",
            "name": path.name,
            "bytes": 0,
            "is_under_scanned_path": True,
            "fullpath": str(path),
        }

    def _list_folder(self, folder: Path) -> List[Dict[str, Any]]:
        stat = folder.stat()
        cache_key = str(folder)
        cached = self.folder_cache.get(cache_key)
        if cached and cached.mtime_ns == stat.st_mtime_ns:
            return cached.files

        files: List[Dict[str, Any]] = []
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        fullpath = Path(entry.path)
                        if entry.is_dir(follow_symlinks=False):
                            files.append(self._file_info(fullpath))
                        elif entry.is_file(follow_symlinks=False) and is_media_file(entry.path):
                            files.append(self._file_info(fullpath))
                    except OSError as exc:
                        logger.debug("Skip unreadable output item %s: %s", entry.path, exc)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        self.folder_cache[cache_key] = _FolderCacheEntry(stat.st_mtime_ns, files)
        return files

    def _top_media_info(self, folder: Path, limit: int = 4) -> List[Dict[str, Any]]:
        media_files: List[Dict[str, Any]] = []
        try:
            entries = sorted(
                os.scandir(folder),
                key=lambda entry: entry.stat().st_ctime,
                reverse=True,
            )
        except OSError as exc:
            logger.debug("Unable to scan directory cover for %s: %s", folder, exc)
            return media_files

        for entry in entries:
            try:
                if not entry.is_file(follow_symlinks=False) or not is_media_file(entry.path):
                    continue
                info = self._file_info(Path(entry.path))
                info["media_type"] = "video" if get_video_type(entry.path) else "image"
                media_files.append(info)
                if len(media_files) >= limit:
                    break
            except Exception as exc:
                logger.debug("Skip directory cover item %s: %s", entry.path, exc)
        return media_files

    def _index_roots(self) -> List[Path]:
        roots: List[Path] = [self.config.output_dir]
        if self.config.input_dir and self.config.input_dir.exists():
            roots.append(self.config.input_dir)
        for item in self.extra_paths:
            try:
                path = Path(item.get("path", "")).expanduser().resolve()
                if path.exists() and path.is_dir():
                    roots.append(path)
            except Exception:
                pass
        unique: List[Path] = []
        seen = set()
        for root in roots:
            key = str(root)
            if key not in seen:
                seen.add(key)
                unique.append(root)
        return unique

    def _ensure_search_index(self) -> None:
        roots = self._index_roots()
        roots_key = "|".join(str(root) for root in roots)
        if not self.search_index or self.search_index_roots_key != roots_key:
            self._rebuild_search_index()

    def _rebuild_search_index(self, force: bool = False) -> None:
        roots = self._index_roots()
        roots_key = "|".join(str(root) for root in roots)
        if self.search_index and self.search_index_roots_key == roots_key and not force:
            return
        files: List[Dict[str, Any]] = []
        tag_map: Dict[str, Dict[str, Any]] = {}
        max_items = 50000
        for root in roots:
            try:
                for dirpath, dirnames, filenames in os.walk(root):
                    # Avoid walking common cache/hidden folders indefinitely.
                    dirnames[:] = [
                        d for d in dirnames
                        if not d.startswith(".") and d not in {"__pycache__", "node_modules", "venv", "env"}
                    ]
                    for filename in filenames:
                        path = Path(dirpath) / filename
                        if not is_media_file(str(path)):
                            continue
                        try:
                            info = self._file_info(path)
                            info["_search_text"] = self._build_search_text(path)
                            info["_tag_ids"] = self._extract_tags_for_item(path, info.get("_search_text", ""), tag_map)
                            files.append(info)
                        except Exception as exc:
                            logger.debug("Skip indexing %s: %s", path, exc)
                        if len(files) >= max_items:
                            logger.warning("ComfyUI-lite search index reached %s items; remaining files skipped", max_items)
                            self.search_index = files
                            self.tag_index = tag_map
                            self.search_index_roots_key = roots_key
                            return
            except Exception as exc:
                logger.debug("Skip indexing root %s: %s", root, exc)
        self.search_index = files
        self.tag_index = tag_map
        self.search_index_roots_key = roots_key

    def _build_search_text(self, path: Path) -> str:
        parts = [path.name, str(path)]
        # Prompt parsing is relatively expensive, so only read it when building
        # the lightweight index. This enables fuzzy text search by prompt/model
        # for ComfyUI images without a full DB.
        if is_image_file(str(path)):
            try:
                parts.append(self._read_comfyui_geninfo(path))
            except Exception:
                pass
        return "\n".join(part for part in parts if part).lower()

    def _extract_tags_for_item(self, path: Path, raw_info: str, tag_map: Dict[str, Dict[str, Any]]) -> List[str]:
        tag_ids: set[str] = set()

        def add_tag(tag_type: str, name: str, display_name: Optional[str] = None) -> None:
            clean_name = str(name or "").strip()
            if not clean_name:
                return
            tag_id = f"{tag_type}:{clean_name.lower()}"
            if tag_id not in tag_map:
                tag_map[tag_id] = {
                    "id": tag_id,
                    "name": clean_name,
                    "display_name": display_name,
                    "type": tag_type,
                    "color": "",
                    "count": 0,
                }
            tag_map[tag_id]["count"] += 1
            tag_ids.add(tag_id)

        fullpath = str(path)
        if is_image_file(fullpath):
            add_tag("Media Type", "image")
        elif is_video_file(fullpath):
            add_tag("Media Type", "video")
        elif is_audio_file(fullpath):
            add_tag("Media Type", "audio")

        suffix = path.suffix.lower().lstrip(".")
        if suffix:
            add_tag("File Extension", suffix)

        if raw_info:
            try:
                params = parse_generation_parameters(raw_info)
                for tag in params.get("pos_prompt", [])[:512]:
                    add_tag("pos", str(tag))
                for lora in params.get("lora", []):
                    add_tag("lora", str(lora.get("name", "")))
                for lyco in params.get("lyco", []):
                    add_tag("lyco", str(lyco.get("name", "")))
                for key, value in params.get("meta", {}).items():
                    key_s = str(key).strip()
                    value_s = str(value).strip()
                    if not key_s or not value_s:
                        continue
                    if key_s in {"Steps", "Seed", "CFG scale", "Sampler", "Scheduler", "Model", "Model hash", "VAE", "Size", "Source Identifier"}:
                        add_tag(key_s, value_s)
            except Exception as exc:
                logger.debug("Failed to extract tags for %s: %s", path, exc)
        return list(tag_ids)

    def _match_images_by_tags(self, body: Dict[str, Any]) -> Dict[str, Any]:
        def as_set(key: str) -> set[str]:
            return {str(v) for v in (body.get(key) or []) if str(v)}

        and_tags = as_set("and_tags")
        or_tags = as_set("or_tags")
        not_tags = as_set("not_tags")
        requested_folders = body.get("folder_paths") or []
        random_sort = bool(body.get("random_sort"))
        try:
            size = max(1, min(int(body.get("size") or 200), 1000))
        except Exception:
            size = 200
        try:
            offset = max(0, int(body.get("cursor") or 0))
        except Exception:
            offset = 0

        folder_roots: List[Path] = []
        for folder in requested_folders:
            try:
                path = self._resolve_path(str(folder), allow_any_existing=True)
                if path.exists():
                    folder_roots.append(path)
            except Exception:
                pass

        custom_images = self._read_custom_tag_state().get("images", {})
        matched: List[Dict[str, Any]] = []
        for item in self.search_index:
            fullpath = item.get("fullpath", "")
            if folder_roots:
                try:
                    p = Path(fullpath).resolve()
                    if not any(self._is_relative_to(p, root) for root in folder_roots):
                        continue
                except Exception:
                    continue
            item_tags = set(item.get("_tag_ids", [])) | {str(v) for v in custom_images.get(self._normalize_custom_tag_image_path(fullpath), [])}
            if and_tags and not and_tags.issubset(item_tags):
                continue
            if or_tags and not (or_tags & item_tags):
                continue
            if not_tags and (not_tags & item_tags):
                continue
            clean = {k: v for k, v in item.items() if not k.startswith("_")}
            matched.append(clean)

        if random_sort:
            import random
            random.shuffle(matched)

        page = matched[offset:offset + size]
        next_offset = offset + len(page)
        return {
            "files": page,
            "cursor": {
                "has_next": next_offset < len(matched),
                "next": str(next_offset),
                "next_cursor": str(next_offset),
            },
        }

    def _search_by_substr(self, body: Dict[str, Any]) -> Dict[str, Any]:
        query = str(body.get("surstr") or "").strip()
        path_only = bool(body.get("path_only"))
        regexp = str(body.get("regexp") or "").strip()
        media_type = str(body.get("media_type") or "all")
        requested_folders = body.get("folder_paths") or []
        try:
            size = max(1, min(int(body.get("size") or 200), 1000))
        except Exception:
            size = 200
        try:
            offset = max(0, int(body.get("cursor") or 0))
        except Exception:
            offset = 0

        import re
        pattern = None
        if regexp and query:
            try:
                pattern = re.compile(query, re.IGNORECASE)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Invalid regular expression: {exc}")

        folder_roots: List[Path] = []
        for folder in requested_folders:
            try:
                path = self._resolve_path(str(folder), allow_any_existing=True)
                if path.exists():
                    folder_roots.append(path)
            except Exception:
                pass

        matched: List[Dict[str, Any]] = []
        query_lower = query.lower()
        for item in self.search_index:
            fullpath = item.get("fullpath", "")
            if folder_roots:
                try:
                    p = Path(fullpath).resolve()
                    if not any(self._is_relative_to(p, root) for root in folder_roots):
                        continue
                except Exception:
                    continue
            if media_type == "image" and not is_image_file(fullpath):
                continue
            if media_type == "video" and not is_video_file(fullpath):
                continue
            haystack = fullpath.lower() if path_only else str(item.get("_search_text", ""))
            if query_lower:
                if pattern:
                    if not pattern.search(haystack):
                        continue
                elif query_lower not in haystack:
                    continue
            clean = {k: v for k, v in item.items() if not k.startswith("_")}
            matched.append(clean)

        page = matched[offset:offset + size]
        next_offset = offset + len(page)
        return {
            "files": page,
            "cursor": {
                "has_next": next_offset < len(matched),
                "next": str(next_offset),
                "next_cursor": str(next_offset),
            },
        }

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _copy_or_move_files(self, file_paths: List[str], dest: str, move: bool, create_dest_folder: bool = False) -> List[Dict[str, Any]]:
        dest_path = self._resolve_path(dest, allow_any_existing=True)
        if create_dest_folder:
            dest_path.mkdir(parents=True, exist_ok=True)
        if not dest_path.exists() or not dest_path.is_dir():
            raise HTTPException(status_code=404, detail="Destination folder does not exist")
        result: List[Dict[str, Any]] = []
        for item in file_paths:
            src = self._resolve_path(item, allow_any_existing=True)
            if not src.exists() or not src.is_file():
                continue
            target = dest_path / src.name
            if target.exists():
                stem, suffix = target.stem, target.suffix
                i = 1
                while target.exists():
                    target = dest_path / f"{stem} ({i}){suffix}"
                    i += 1
            if move:
                shutil.move(str(src), str(target))
            else:
                shutil.copy2(str(src), str(target))
            result.append(self._file_info(target))
        self.folder_cache.clear()
        return result

    def _global_extra_paths(self) -> List[Dict[str, Any]]:
        output = str(self.config.output_dir)
        paths = [{"path": output, "type": "walk+scanned-fixed+cli_access_only", "name": "ComfyUI 输出文件夹"}]
        paths.extend(
            {
                "path": item["path"],
                "type": "+".join(item.get("types") or ["scanned-fixed"]),
                "name": item.get("alias") or Path(item["path"]).name or item["path"],
                "alias": item.get("alias"),
            }
            for item in self.extra_paths
        )
        return paths

    def _read_extra_paths(self) -> List[Dict[str, Any]]:
        path = self._extra_paths_file()
        if not path or not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.debug("Failed to read extra paths: %s", exc)
            return []

    def _write_extra_paths(self) -> None:
        path = self._extra_paths_file()
        if not path:
            raise HTTPException(status_code=500, detail="Cache directory is not available")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.extra_paths, ensure_ascii=False, indent=2), encoding="utf-8")

    def _extra_paths_file(self) -> Optional[Path]:
        if not self.cache_base_dir:
            return None
        return Path(self.cache_base_dir) / "iib_cache" / "comfyui_lite" / "extra_paths.json"

    def _custom_tags_file(self) -> Optional[Path]:
        if not self.cache_base_dir:
            return None
        return Path(self.cache_base_dir) / "iib_cache" / "comfyui_lite" / "custom_tags.json"

    def _read_custom_tag_state(self) -> Dict[str, Any]:
        path = self._custom_tags_file()
        if not path or not path.exists():
            return {"next_id": 1, "tags": [], "images": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("custom tag state must be an object")
            data.setdefault("next_id", 1)
            data.setdefault("tags", [])
            data.setdefault("images", {})
            if not isinstance(data["tags"], list):
                data["tags"] = []
            if not isinstance(data["images"], dict):
                data["images"] = {}
            return data
        except Exception as exc:
            logger.debug("Failed to read custom tags: %s", exc)
            return {"next_id": 1, "tags": [], "images": {}}

    def _write_custom_tag_state(self, state: Dict[str, Any]) -> None:
        path = self._custom_tags_file()
        if not path:
            raise HTTPException(status_code=500, detail="Cache directory is not available")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _normalize_custom_tag_image_path(self, path: Any) -> str:
        raw = str(path or "").strip()
        if not raw:
            return ""
        try:
            return str(Path(raw).expanduser().resolve())
        except Exception:
            return os.path.normpath(raw)

    def _normalize_tag_id(self, tag_id: Any) -> Optional[int]:
        try:
            return int(tag_id)
        except Exception:
            return None

    def _get_all_custom_tags(self) -> List[Dict[str, Any]]:
        state = self._read_custom_tag_state()
        tags = []
        for tag in state.get("tags", []):
            if not isinstance(tag, dict):
                continue
            tags.append({
                "id": tag.get("id"),
                "name": tag.get("name") or "",
                "display_name": tag.get("display_name"),
                "type": "custom",
                "color": tag.get("color") or "",
                "count": int(tag.get("count") or 0),
            })
        return tags

    def _add_custom_tag(self, tag_name: str) -> Dict[str, Any]:
        state = self._read_custom_tag_state()
        clean_name = tag_name.strip()
        for tag in state.get("tags", []):
            if str(tag.get("name", "")).strip().lower() == clean_name.lower():
                return tag
        next_id = int(state.get("next_id") or 1)
        tag = {"id": next_id, "name": clean_name, "display_name": None, "type": "custom", "color": "", "count": 0}
        state["next_id"] = next_id + 1
        state.setdefault("tags", []).append(tag)
        self._write_custom_tag_state(state)
        return tag

    def _update_custom_tag(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        state = self._read_custom_tag_state()
        tag_id = self._normalize_tag_id(payload.get("id"))
        if tag_id is None:
            raise HTTPException(status_code=400, detail="Invalid tag id")
        for tag in state.get("tags", []):
            if self._normalize_tag_id(tag.get("id")) == tag_id:
                if "name" in payload:
                    name = str(payload.get("name") or "").strip()
                    if name:
                        tag["name"] = name
                if "display_name" in payload:
                    tag["display_name"] = payload.get("display_name")
                if "color" in payload:
                    tag["color"] = str(payload.get("color") or "")
                self._write_custom_tag_state(state)
                return tag
        raise HTTPException(status_code=404, detail="Tag not found")

    def _remove_custom_tag(self, tag_id: Any) -> None:
        state = self._read_custom_tag_state()
        normalized = self._normalize_tag_id(tag_id)
        if normalized is None:
            return
        state["tags"] = [tag for tag in state.get("tags", []) if self._normalize_tag_id(tag.get("id")) != normalized]
        tag_id_s = str(normalized)
        for image_path, tag_ids in list(state.get("images", {}).items()):
            state["images"][image_path] = [v for v in tag_ids if str(v) != tag_id_s]
            if not state["images"][image_path]:
                del state["images"][image_path]
        self._write_custom_tag_state(state)

    def _recount_custom_tags(self, state: Dict[str, Any]) -> None:
        counts: Dict[str, int] = {}
        for tag_ids in state.get("images", {}).values():
            for tag_id in tag_ids or []:
                counts[str(tag_id)] = counts.get(str(tag_id), 0) + 1
        for tag in state.get("tags", []):
            tag["count"] = counts.get(str(tag.get("id")), 0)

    def _get_selected_custom_tag_ids(self, path: Any) -> List[str]:
        key = self._normalize_custom_tag_image_path(path)
        if not key:
            return []
        state = self._read_custom_tag_state()
        return [str(v) for v in state.get("images", {}).get(key, [])]

    def _get_selected_custom_tags(self, path: Any) -> List[Dict[str, Any]]:
        ids = set(self._get_selected_custom_tag_ids(path))
        return [tag for tag in self._get_all_custom_tags() if str(tag.get("id")) in ids]

    def _toggle_custom_tag_to_img(self, tag_id: Any, img_path: Any) -> Dict[str, bool]:
        normalized = self._normalize_tag_id(tag_id)
        image_key = self._normalize_custom_tag_image_path(img_path)
        if normalized is None or not image_key:
            raise HTTPException(status_code=400, detail="Invalid tag or image path")
        state = self._read_custom_tag_state()
        if not any(self._normalize_tag_id(tag.get("id")) == normalized for tag in state.get("tags", [])):
            raise HTTPException(status_code=404, detail="Tag not found")
        tag_id_s = str(normalized)
        selected = [str(v) for v in state.setdefault("images", {}).get(image_key, [])]
        is_remove = tag_id_s in selected
        if is_remove:
            selected = [v for v in selected if v != tag_id_s]
        else:
            selected.append(tag_id_s)
        if selected:
            state["images"][image_key] = selected
        else:
            state["images"].pop(image_key, None)
        self._recount_custom_tags(state)
        self._write_custom_tag_state(state)
        return {"is_remove": is_remove}

    def _batch_update_image_tag(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        tag_id = self._normalize_tag_id(payload.get("tag_id"))
        action = str(payload.get("action") or "add")
        paths = payload.get("paths") or payload.get("file_paths") or []
        if tag_id is None or action not in {"add", "remove"}:
            raise HTTPException(status_code=400, detail="Invalid request")
        state = self._read_custom_tag_state()
        if not any(self._normalize_tag_id(tag.get("id")) == tag_id for tag in state.get("tags", [])):
            raise HTTPException(status_code=404, detail="Tag not found")
        tag_id_s = str(tag_id)
        images = state.setdefault("images", {})
        updated = 0
        for raw_path in paths:
            image_key = self._normalize_custom_tag_image_path(raw_path)
            if not image_key:
                continue
            selected = [str(v) for v in images.get(image_key, [])]
            if action == "add" and tag_id_s not in selected:
                selected.append(tag_id_s)
                updated += 1
            elif action == "remove" and tag_id_s in selected:
                selected = [v for v in selected if v != tag_id_s]
                updated += 1
            if selected:
                images[image_key] = selected
            else:
                images.pop(image_key, None)
        self._recount_custom_tags(state)
        self._write_custom_tag_state(state)
        return {"success": True, "updated": updated}


    def _upsert_extra_path(self, path: str, types: List[str], alias: Optional[str] = None) -> None:
        norm = str(Path(path).expanduser().resolve())
        existing = next((item for item in self.extra_paths if item.get("path") == norm), None)
        if existing:
            merged = list(dict.fromkeys((existing.get("types") or []) + (types or [])))
            existing["types"] = merged or ["scanned-fixed"]
            if alias is not None:
                existing["alias"] = alias
        else:
            self.extra_paths.append({"path": norm, "types": types or ["scanned-fixed"], "alias": alias})
        self._write_extra_paths()

    def _remove_extra_path(self, path: str, types: List[str]) -> None:
        norm = str(Path(path).expanduser().resolve())
        for item in list(self.extra_paths):
            if item.get("path") != norm:
                continue
            if types:
                item["types"] = [t for t in item.get("types", []) if t not in types]
            if not types or not item.get("types"):
                self.extra_paths.remove(item)
        self._write_extra_paths()

    def _image_info_without_exif(self, path: Path) -> Dict[str, str]:
        with Image.open(path) as img:
            return {k: str(v) for k, v in img.info.items() if not k.lower().startswith("exif")}


    def _thumbnail_response(self, path: Path, t: str, size: str) -> FileResponse:
        if not self.cache_base_dir:
            raise HTTPException(status_code=500, detail="Cache directory is not available")
        try:
            w, h = size.split("x")
            max_size = (int(w), int(h))
            if max(max_size) > 1024 or min(max_size) <= 0:
                raise ValueError("invalid thumbnail size")
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid thumbnail size")

        hash_dir = hashlib.md5((str(path) + t).encode("utf-8")).hexdigest()
        cache_dir = Path(self.cache_base_dir) / "iib_cache" / "comfyui_lite" / hash_dir
        cache_path = cache_dir / f"{size}.jpg"
        headers = {"Cache-Control": "max-age=31536000", "ETag": hash_dir + size}
        if cache_path.exists():
            return FileResponse(str(cache_path), media_type="image/jpeg", headers=headers)

        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            with Image.open(path) as img:
                img = ImageOps.exif_transpose(img)
                img.thumbnail(max_size)
                if img.mode in ("RGBA", "LA"):
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    alpha = img.getchannel("A") if "A" in img.getbands() else None
                    background.paste(img.convert("RGBA"), mask=alpha)
                    img = background
                elif img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(cache_path, "jpeg", quality=82, optimize=False)
            return FileResponse(str(cache_path), media_type="image/jpeg", headers=headers)
        except Exception as exc:
            logger.error("Failed to generate image thumbnail. path=%s error=%s", path, exc)
            raise HTTPException(status_code=415, detail=f"Unable to generate image thumbnail: {exc}")

    def _read_comfyui_workflow(self, path: Path) -> Dict[str, Any]:
        if not path.exists() or not path.is_file() or not is_image_file(str(path)):
            return {}
        try:
            with Image.open(path) as img:
                if img.format == "PNG":
                    workflow = img.info.get("workflow")
                    prompt = img.info.get("prompt")
                    return {
                        "workflow": json.loads(workflow) if isinstance(workflow, str) and workflow else None,
                        "prompt": json.loads(prompt) if isinstance(prompt, str) and prompt else None,
                    }
                if img.format in ("WEBP", "JPEG", "JPG"):
                    exif = img.info.get("exif")
                    if not exif:
                        return {}
                    split = [x.decode("utf-8", errors="ignore") for x in exif.split(b"\x00")]
                    workflow_str = next((x for x in split if x.lower().startswith("workflow:")), None)
                    prompt_str = next((x for x in split if x.lower().startswith("prompt:")), None)
                    return {
                        "workflow": json.loads(workflow_str.split(":", 1)[1]) if workflow_str else None,
                        "prompt": json.loads(prompt_str.split(":", 1)[1]) if prompt_str else None,
                    }
        except Exception as exc:
            logger.debug("Failed to read ComfyUI workflow for %s: %s", path, exc)
        return {}

    def _read_comfyui_geninfo(self, path: Path) -> str:
        if not path.exists() or not path.is_file() or not is_image_file(str(path)):
            return ""
        override = self._geninfo_override_path(path)
        if override and override.exists():
            try:
                return override.read_text(encoding="utf-8")
            except Exception as exc:
                logger.debug("Failed to read ComfyUI geninfo override for %s: %s", path, exc)
        try:
            with Image.open(path) as img:
                if ComfyUIParser.test(img, str(path)):
                    return ComfyUIParser.parse(img, str(path)).raw_info or ""
                return ""
        except Exception as exc:
            logger.debug("Failed to read ComfyUI geninfo for %s: %s", path, exc)
            return ""

    def _read_app_fe_setting(self, name: str) -> Dict[str, Any]:
        path = self._app_fe_setting_path(name)
        if not path or not path.exists():
            return {}
        try:
            value = path.read_text(encoding="utf-8")
            return json.loads(value) if value else {}
        except Exception as exc:
            logger.debug("Failed to read app_fe_setting %s: %s", name, exc)
            return {}

    def _read_all_app_fe_settings(self) -> Dict[str, Dict[str, Any]]:
        directory = self._app_fe_setting_dir()
        if not directory or not directory.exists():
            return {}
        settings: Dict[str, Dict[str, Any]] = {}
        for item in directory.glob("*.json"):
            settings[item.stem] = self._read_app_fe_setting(item.stem)
        return settings

    def _write_app_fe_setting(self, name: str, value: str) -> None:
        path = self._app_fe_setting_path(name)
        if not path:
            raise HTTPException(status_code=500, detail="Cache directory is not available")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Validate JSON before persisting; frontend expects parsed objects on next load.
        json.loads(value or "{}")
        path.write_text(value or "{}", encoding="utf-8")

    def _delete_app_fe_setting(self, name: str) -> None:
        path = self._app_fe_setting_path(name)
        if path and path.exists():
            path.unlink()

    def _app_fe_setting_path(self, name: str) -> Optional[Path]:
        directory = self._app_fe_setting_dir()
        if not directory:
            return None
        safe_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
        return directory / f"{safe_name}.json"

    def _app_fe_setting_dir(self) -> Optional[Path]:
        if not self.cache_base_dir:
            return None
        return Path(self.cache_base_dir) / "iib_cache" / "comfyui_lite" / "app_fe_setting"

    def _write_geninfo_override(self, path: Path, exif: str) -> None:
        override = self._geninfo_override_path(path)
        if not override:
            raise HTTPException(status_code=500, detail="Cache directory is not available")
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(exif, encoding="utf-8")

    def _geninfo_override_path(self, path: Path) -> Optional[Path]:
        if not self.cache_base_dir:
            return None
        stat = path.stat()
        key = hashlib.md5(f"{path}|{stat.st_mtime_ns}".encode("utf-8")).hexdigest()
        return Path(self.cache_base_dir) / "iib_cache" / "comfyui_lite" / "geninfo_overrides" / f"{key}.txt"

    def _open_path_with_os(self, path: Path) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to open path: {exc}")

    def _long_cache_headers(self, filename: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "Cache-Control": "public, max-age=31536000",
            "Expires": (datetime.now() + timedelta(days=365)).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        }
        if filename:
            encoded = urllib.parse.quote(filename.encode("utf-8"))
            headers["Content-Disposition"] = f"inline; filename*=UTF-8''{encoded}"
        return headers


def create_comfyui_lite_app(output_dir: str | os.PathLike[str], input_dir: str | os.PathLike[str] | None = None, base: str = "/iib") -> FastAPI:
    return ComfyUILiteApi(ComfyUILiteConfig(output_dir=output_dir, input_dir=input_dir, base=base)).create_app()
