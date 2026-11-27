"""Bus mode's pages: the rides (who got on where and off where), and its settings."""
from __future__ import annotations

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse


def mount_bus(app: FastAPI, box, render, need_done, api_admin, same_origin, settings_page) -> None:
    rides = box.passengers

    def page(request: Request, state: str = "", **extra):
        cams = [dict(r) | {"role": rides.camera_role(r["serial"])} for r in box.db.all("SELECT * FROM cameras ORDER BY id")]
        ctx = dict(active="passengers", rides=rides.list(limit=200, state=state), summary=rides.summary(), state=state,
                   bus=box.devices.mode() == "bus", money=rides.settings()["fare_currency"],
                   latest=box.devices.latest_position(), faces_ready=box.faces_ready(), faces_error=box.faces_error(),
                   cameras=cams, saved=None, error=None)
        ctx.update(extra)
        return render(request, "passengers.html", **ctx)

    @app.get("/passengers", response_class=HTMLResponse, dependencies=[Depends(need_done)])
    def passengers(request: Request, state: str = ""):
        return page(request, state)

    @app.get("/rides/{ride_id}/{kind}.jpg", dependencies=[Depends(api_admin)])
    def ride_picture(ride_id: int, kind: str):
        p = rides.file_for(ride_id, kind)
        if p is None:
            raise HTTPException(404, "No such picture.")
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/passengers/close-all", dependencies=[Depends(need_done), Depends(same_origin)])
    def close_all(request: Request):
        n = rides.close_all()
        return page(request, saved=f"{n} ride(s) closed here: everybody who was on board has got off.")

    @app.post("/passengers/{ride_id}/delete", dependencies=[Depends(need_done), Depends(same_origin)])
    def delete(ride_id: int):
        rides.delete(ride_id)
        return RedirectResponse("/passengers", status_code=303)

    @app.get("/passengers/export.csv", dependencies=[Depends(need_done)])
    def export():
        return PlainTextResponse(rides.export_csv(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=rides.csv"})

    @app.post("/passengers/camera-role", dependencies=[Depends(need_done), Depends(same_origin)])
    def camera_role(request: Request, serial: str = Form(""), role: str = Form("both")):
        if not box.db.one("SELECT id FROM cameras WHERE serial = ?", (serial,)):
            raise HTTPException(404, "No such camera.")
        try:
            rides.set_camera_role(serial, role)
        except ValueError as e:
            return page(request, error=str(e))
        return page(request, saved="Saved.")

    # -- settings ------------------------------------------------------------------------------
    @app.post("/settings/mode", dependencies=[Depends(need_done), Depends(same_origin)])
    def set_mode(request: Request, mode: str = Form("parking"), consent: str = Form("")):
        if mode not in ("parking", "bus"):
            raise HTTPException(400, "Unknown mode.")
        if mode == "bus" and consent != "1":
            return settings_page(request, error="Bus mode records people's faces. Tick the box to confirm that passengers "
                                                "are told and that you are allowed to do this, then save again.")
        if mode == "bus" and not box.faces_ready():
            return settings_page(request, error="Bus mode is not available: " + (box.faces_error() or "the face models are missing."))
        box.db.put("mode", mode)
        box.db.event("mode", mode)
        return settings_page(request, saved="Now recording bus passengers. Each gateway is told at its next sign-in and then "
                                            "starts reporting its position." if mode == "bus" else "Now recording number plates.")

    @app.post("/settings/bus", dependencies=[Depends(need_done), Depends(same_origin)])
    def set_bus(request: Request, fare_base: str = Form(""), fare_per_km: str = Form(""), fare_currency: str = Form(""),
                face_threshold: str = Form(""), min_ride_s: str = Form(""), max_ride_h: str = Form(""),
                tz_minutes: str = Form(""), rides_retention_days: str = Form("")):
        try:
            rides.configure(fare_base=fare_base, fare_per_km=fare_per_km, fare_currency=fare_currency,
                            face_threshold=face_threshold, min_ride_s=min_ride_s, max_ride_h=max_ride_h,
                            tz_minutes=tz_minutes, rides_retention_days=rides_retention_days)
        except ValueError as e:
            return settings_page(request, error=str(e) if "between" in str(e) else "Those must be numbers.")
        return settings_page(request, saved="Bus settings saved.")

    @app.post("/settings/rides-clear", dependencies=[Depends(need_done), Depends(same_origin)])
    def clear(request: Request):
        return settings_page(request, saved=f"Deleted {rides.delete_all()} rides and their pictures.")
