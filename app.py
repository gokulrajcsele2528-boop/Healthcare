"""
TriVanta AI - Application entry point.

Run with:  python app.py
Starts the full application (API + frontend) at http://localhost:8000
No second terminal, no separate build step required.
"""
import json
import logging
import os
import uvicorn
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.backend.config import APP_HOST, APP_PORT, FRONTEND_DIR, AI_ENABLED, DISCLAIMER
from src.backend.database.db import init_db
from src.backend.database import models as db
from src.backend.triage import pipeline

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trivanta")

app = FastAPI(title="TriVanta AI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Startup: load rules, database, retrieval index (section 48 - do the
# expensive work once at startup, not per-request).
# ---------------------------------------------------------------------------
@app.on_event("startup")
def on_startup():
    init_db()
    try:
        from src.backend.retrieval.retriever import get_retriever
        get_retriever()  # warms the local index once
        logger.info("Retrieval index ready. AI (Gemini) enabled: %s", AI_ENABLED)
    except Exception as e:
        logger.warning("Retrieval index warm-up failed (will retry lazily): %s", e)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class StartIntakeRequest(BaseModel):
    message: str
    user_id: str = "patient_demo"


class AnswerRequest(BaseModel):
    field_key: str
    answer: str


class ReviewRequest(BaseModel):
    reviewer_name: str = "staff_demo"


class StaffLoginRequest(BaseModel):
    staff_id: str
    role: str = "staff"


class StaffAssignmentRequest(BaseModel):
    user_id: str
    staff_id: str
    admin_id: str


class AccessIdRequest(BaseModel):
    user_id: str
    admin_id: str
    access_id: str = ""


class AdminAccountCreateRequest(BaseModel):
    admin_id: str
    name: str
    email: str
    password: str
    role: str
    access_id: str = ""


class AccessLoginRequest(BaseModel):
    access_id: str
    role: str


# ---------------------------------------------------------------------------
# API: Patient intake
# ---------------------------------------------------------------------------
@app.post("/api/intake/start")
def api_start_intake(req: StartIntakeRequest):
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="Please describe what's bothering you before continuing.")
    try:
        return pipeline.start_intake(req.user_id, req.message.strip())
    except Exception as e:
        logger.exception("Intake failed")
        return JSONResponse(status_code=200, content={
            "status": "SYSTEM_ERROR",
            "message": "AI processing is temporarily unavailable. Please continue with human review.",
            "detail": str(e),
        })


@app.post("/api/intake/{assessment_id}/answer")
def api_submit_answer(assessment_id: str, req: AnswerRequest):
    if not req.answer or not req.answer.strip():
        raise HTTPException(status_code=400, detail="Please provide an answer.")
    try:
        result = pipeline.submit_answer(assessment_id, req.field_key, req.answer.strip())
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Answer submission failed")
        return JSONResponse(status_code=200, content={
            "status": "SYSTEM_ERROR",
            "message": "AI processing is temporarily unavailable. Please continue with human review.",
            "detail": str(e),
        })


@app.get("/api/intake/{assessment_id}")
def api_get_case_patient_view(assessment_id: str):
    case = pipeline.get_case_detail(assessment_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    return case


# ---------------------------------------------------------------------------
# API: Staff / human review dashboard
# ---------------------------------------------------------------------------
@app.get("/api/assessments")
def api_list_assessments(status: str = None, review_status: str = None):
    return db.list_assessments(status=status, review_status=review_status)


@app.get("/api/assessments/{assessment_id}")
def api_get_case_detail(assessment_id: str):
    case = pipeline.get_case_detail(assessment_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    return case


@app.post("/api/assessments/{assessment_id}/review/start")
def api_start_review(assessment_id: str):
    return pipeline.set_under_review(assessment_id)


@app.post("/api/assessments/{assessment_id}/review/complete")
def api_complete_review(assessment_id: str, req: ReviewRequest):
    return pipeline.mark_reviewed(assessment_id, req.reviewer_name)


@app.get("/api/dashboard/stats")
def api_dashboard_stats():
    return db.get_stats()


@app.get("/api/staff/users")
def api_staff_users(role: str = None):
    """Return non-sensitive account details for the staff directory."""
    if role not in (None, "patient", "staff"):
        raise HTTPException(status_code=400, detail="Unsupported user role.")
    return db.list_users(role=role)


@app.post("/api/admin/staff/assign")
def api_assign_staff_id(req: StaffAssignmentRequest):
    expected_admin_id = os.getenv("TRIVANTA_ADMIN_ID", "TRIVANTA_ADMIN_001")
    if req.admin_id.strip().upper() != expected_admin_id.upper():
        raise HTTPException(status_code=403, detail="Admin access required.")
    try:
        return {"user": db.assign_staff_id(req.user_id, req.staff_id)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/admin/staff/{user_id}/id")
def api_remove_staff_id(user_id: str, admin_id: str):
    expected_admin_id = os.getenv("TRIVANTA_ADMIN_ID", "TRIVANTA_ADMIN_001")
    if admin_id.strip().upper() != expected_admin_id.upper():
        raise HTTPException(status_code=403, detail="Admin access required.")
    try:
        return {"user": db.remove_staff_id(user_id)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/access-ids/generate")
def api_generate_access_id(req: AccessIdRequest):
    expected_admin_id = os.getenv("TRIVANTA_ADMIN_ID", "TRIVANTA_ADMIN_001")
    if req.admin_id.strip().upper() != expected_admin_id.upper():
        raise HTTPException(status_code=403, detail="Admin access required.")
    try:
        return {"user": db.assign_access_id(req.user_id, req.access_id or None)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/accounts/create")
def api_admin_create_account(req: AdminAccountCreateRequest):
    expected_admin_id = os.getenv("TRIVANTA_ADMIN_ID", "TRIVANTA_ADMIN_001")
    if req.admin_id.strip().upper() != expected_admin_id.upper():
        raise HTTPException(status_code=403, detail="Admin access required.")
    try:
        return {"user": db.create_admin_account(req.name, req.email, req.password, req.role, req.access_id or None)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/admin/access-ids/{user_id}")
def api_remove_access_id(user_id: str, admin_id: str):
    expected_admin_id = os.getenv("TRIVANTA_ADMIN_ID", "TRIVANTA_ADMIN_001")
    if admin_id.strip().upper() != expected_admin_id.upper():
        raise HTTPException(status_code=403, detail="Admin access required.")
    try:
        return {"user": db.remove_access_id(user_id)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.delete("/api/admin/accounts/{user_id}")
def api_delete_account(user_id: str, admin_id: str):
    expected_admin_id = os.getenv("TRIVANTA_ADMIN_ID", "TRIVANTA_ADMIN_001")
    if admin_id.strip().upper() != expected_admin_id.upper():
        raise HTTPException(status_code=403, detail="Admin access required.")
    try:
        deleted = db.delete_user_account(user_id)
        return {"deleted": True, "user": deleted}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/auth/access-login")
def api_access_login(req: AccessLoginRequest):
    if req.role not in ("patient", "staff"):
        raise HTTPException(status_code=403, detail="Access ID login is for patient or staff accounts.")
    user = db.get_user_by_access_id(req.access_id)
    if not user or user["role"] != req.role:
        raise HTTPException(status_code=401, detail="Invalid or unassigned access ID.")
    return {"user": user, "user_id": user["id"]}


@app.post("/api/auth/staff-login")
def api_staff_login(req: StaffLoginRequest):
    if req.role != "staff":
        raise HTTPException(status_code=403, detail="Staff ID login is only for staff accounts.")
    user = db.get_user_by_staff_id(req.staff_id)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or unassigned staff ID.")
    return {"user": user, "user_id": user["id"]}


# ---------------------------------------------------------------------------
# API: Demo mode (section 46) - safe synthetic scenarios for judges
# ---------------------------------------------------------------------------
DEMO_PATH = Path(__file__).parent / "demo" / "scenarios.json"


@app.get("/api/demo/scenarios")
def api_demo_scenarios():
    if not DEMO_PATH.exists():
        return []
    return json.loads(DEMO_PATH.read_text(encoding="utf-8"))


@app.get("/api/system/status")
def api_system_status():
    return {
        "app": "TriVanta AI",
        "ai_enabled": AI_ENABLED,
        "disclaimer": DISCLAIMER,
        "supported_complaints": [
            "fever", "injury", "chest_pain", "breathing_difficulty", "abdominal_pain",
        ],
    }


# ---------------------------------------------------------------------------
# Frontend (served by this same Python app - no separate build step)
# ---------------------------------------------------------------------------
STATIC_DIR = FRONTEND_DIR / "dist"
app.mount("/assets", StaticFiles(directory=str(STATIC_DIR)), name="assets")


@app.get("/")
def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/staff")
def serve_staff_dashboard():
    return FileResponse(str(STATIC_DIR / "staff.html"))


@app.get("/admin")
def serve_admin_login():
    return FileResponse(str(STATIC_DIR / "admin.html"))


@app.get("/admin/dashboard")
def serve_admin_dashboard():
    return FileResponse(str(STATIC_DIR / "staff.html"))


@app.get("/style.css")
def serve_staff_styles():
    return FileResponse(str(STATIC_DIR / "style.css"), media_type="text/css")


@app.get("/staff.js")
def serve_staff_script():
    return FileResponse(str(STATIC_DIR / "staff.js"), media_type="application/javascript")


@app.get("/trivanta-logo.svg")
def serve_staff_logo():
    return FileResponse(str(STATIC_DIR / "trivanta-logo.svg"), media_type="image/svg+xml")


@app.get("/logo.png")
def serve_brand_logo():
    return FileResponse(str(FRONTEND_DIR / "logo.png"), media_type="image/png")


@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    # Let unmatched non-API paths fall back to the SPA shell.
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    uvicorn.run("app:app", host=APP_HOST, port=APP_PORT, reload=False)
