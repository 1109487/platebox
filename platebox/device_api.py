"""What a gateway talks to (/device/v1/..., its own token) and the page where gateways and their firmware are managed."""
from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .devices import MAX_FIRMWARE_BYTES, FirmwareError

log = logging.getLogger("platebox.device_api")
MAX_BODY = 64 * 1024


def mount_devices(app: FastAPI, box, render, need_done, same_origin) -> None:
    devices = box.devices

    def gateway(request: Request):
        """The gateway that is calling, from its token. Wrong tokens are slowed down per address, like wrong passwords."""
        key = "dev:" + (request.client.host if request.client else "?")
        if box.throttle.blocked(key):
            raise HTTPException(429, "Too many wrong tokens. Wait a few minutes.")
        auth = request.headers.get("authorization", "")
        dev = devices.auth(auth[7:].strip()) if auth.lower().startswith("bearer ") else None
        if dev is None:
            box.throttle.fail(key)
            raise HTTPException(401, "Unknown gateway token.")
        return dev

    async def body_json(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "Too much.")
        try:
            data = await request.json() if raw else {}
        except ValueError:
            raise HTTPException(400, "Not JSON.")
        if not isinstance(data, dict):
            raise HTTPException(400, "Not a JSON object.")
        return data

    # -- the gateway's side ----------------------------------------------------------------------
    @app.post("/device/v1/checkin")
    async def checkin(request: Request, dev=Depends(gateway)):
        info = await body_json(request)
        ip = request.client.host if request.client else ""
        answer = devices.checkin(dev, info, ip)
        if isinstance(info.get("positions"), list):
            answer["positions_saved"] = devices.add_positions(dev["id"], info["positions"])
        return answer

    @app.post("/device/v1/positions")
    async def positions(request: Request, dev=Depends(gateway)):
        data = await body_json(request)
        points = data.get("positions")
        if not isinstance(points, list):
            raise HTTPException(400, "positions must be a list.")
        return {"ok": True, "saved": devices.add_positions(dev["id"], points)}

    @app.get("/device/v1/firmware/{fw_id}")
    def firmware(fw_id: int, dev=Depends(gateway)):
        p = devices.firmware_file(fw_id)
        if p is None:
            raise HTTPException(404, "No such firmware.")
        return FileResponse(p, media_type="application/octet-stream")

    # -- the administrator's side ------------------------------------------------------------------
    def page(request: Request, **extra):
        ctx = dict(active="gateways", gateways=devices.list(), firmware=devices.firmware_list(), mode=devices.mode(),
                   latest=devices.latest_position(), saved=None, error=None, new_token=None, new_name=None,
                   host=box.cfg.public_host or request.headers.get("host", ""))
        ctx.update(extra)
        return render(request, "gateways.html", **ctx)

    @app.get("/gateways", response_class=HTMLResponse, dependencies=[Depends(need_done)])
    def gateways(request: Request):
        return page(request)

    @app.post("/gateways/add", dependencies=[Depends(need_done), Depends(same_origin)])
    def add(request: Request, name: str = Form("")):
        _, token = devices.create(name)
        return page(request, new_token=token, new_name=" ".join(name.split())[:60] or "Gateway")

    @app.post("/gateways/{dev_id}/token", dependencies=[Depends(need_done), Depends(same_origin)])
    def retoken(request: Request, dev_id: int):
        token = devices.new_token(dev_id)
        if token is None:
            raise HTTPException(404, "No such gateway.")
        return page(request, new_token=token, new_name="this gateway")

    @app.post("/gateways/{dev_id}/remove", dependencies=[Depends(need_done), Depends(same_origin)])
    def remove(dev_id: int):
        devices.remove(dev_id)
        return RedirectResponse("/gateways", status_code=303)

    @app.post("/gateways/firmware", dependencies=[Depends(need_done), Depends(same_origin)])
    async def upload(request: Request, file: UploadFile = File(...), note: str = Form("")):
        image = await file.read(MAX_FIRMWARE_BYTES + 1)
        try:
            fw = devices.add_firmware(image, note)
        except FirmwareError as e:
            return page(request, error=str(e))
        return page(request, saved=f"Firmware {fw['model']} {fw['version']} is here. Each gateway of that kind takes it "
                                   "the next time it signs in (within an hour, or at once with `ota check` on its console).")

    @app.post("/gateways/firmware/{fw_id}/remove", dependencies=[Depends(need_done), Depends(same_origin)])
    def remove_firmware(fw_id: int):
        devices.remove_firmware(fw_id)
        return RedirectResponse("/gateways", status_code=303)
