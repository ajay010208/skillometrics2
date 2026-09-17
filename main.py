"""Unified SkilloMetrics backend — FastAPI + SQLAlchemy on the SAME dev.db.

Architecture being prototyped (vs the current Express API + FastAPI AI split):
  - One process, one language: HTTP API + AI logic side by side.
  - AI "calls" become plain function calls (no HTTP hop, no envelope unwrap).
  - SQLAlchemy maps the EXISTING Prisma-created SQLite schema (table/column
    names preserved) so both backends run against one database.

Run:  python main.py          (port 5001 to sit beside the real stack)
"""
import base64
import json
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import (String, Integer, Float, Boolean, DateTime, Text,
                        create_engine, select, func, desc)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column,
                            relationship, sessionmaker, Session)

load_dotenv("../.env")  # reuse root env (Supabase URL/key, ports)

DB_PATH = os.getenv("UNIFIED_DB", "../apps/api/prisma/dev.db")
engine = create_engine(
    f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

PORT = int(os.getenv("UNIFIED_PORT", "5001"))
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
AI_MODE = "llm" if os.getenv("AI_API_KEY") else "fallback"

app = FastAPI(title="SkilloMetrics Unified Backend", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# Models — mirror of the Prisma schema (table + column names preserved)
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


def J(v):
    return json.loads(v) if v else None


def JS(v):
    return json.dumps(v) if v is not None else None


def now_iso() -> str:
    """Prisma-compatible TEXT timestamp (ms precision, Z suffix)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Profile(Base):
    __tablename__ = "Profile"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String)
    # Prisma writes DateTime as TEXT in a format SQLAlchemy's datetime
    # processor can't parse — and the prototype never needs it as datetime.
    createdAt: Mapped[str | None] = mapped_column(String, nullable=True)


class Trainee(Base):
    __tablename__ = "Trainee"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    profileId: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String)
    district: Mapped[str] = mapped_column(String)
    weeklyHours: Mapped[int] = mapped_column(Integer, default=10)
    consentTracking: Mapped[bool] = mapped_column(Boolean, default=True)
    consentRecruiterVisible: Mapped[bool] = mapped_column(Boolean, default=False)
    targetJobId: Mapped[str | None] = mapped_column(String, nullable=True)


class Skill(Base):
    __tablename__ = "Skill"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)


class TargetJob(Base):
    __tablename__ = "TargetJob"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String)
    family: Mapped[str] = mapped_column(String)
    requiredSkills: Mapped[str] = mapped_column(Text)  # JSON [{skillId,weight,minLevel}]


class TraineeSkill(Base):
    __tablename__ = "TraineeSkill"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    traineeId: Mapped[str] = mapped_column(String)
    skillId: Mapped[str] = mapped_column(String)
    level: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String)
    updatedAt: Mapped[str | None] = mapped_column(String, nullable=True)
    __table_args__ = {"sqlite_autoincrement": False}


class LearningResource(Base):
    __tablename__ = "LearningResource"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    skillId: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    platform: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(String)
    language: Mapped[str] = mapped_column(String)
    cost: Mapped[str] = mapped_column(String)
    durationHours: Mapped[float] = mapped_column(Float)
    rating: Mapped[float] = mapped_column(Float, default=4.5)


class RoadmapItem(Base):
    __tablename__ = "RoadmapItem"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    traineeId: Mapped[str] = mapped_column(String)
    skillId: Mapped[str] = mapped_column(String)
    resourceId: Mapped[str | None] = mapped_column(String, nullable=True)
    estHours: Mapped[float] = mapped_column(Float)
    weeklyHours: Mapped[int] = mapped_column(Integer)
    targetWeeks: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="pending")
    order: Mapped[int] = mapped_column(Integer, default=0)


class Assessment(Base):
    __tablename__ = "Assessment"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    traineeId: Mapped[str] = mapped_column(String)
    skillId: Mapped[str] = mapped_column(String)
    questions: Mapped[str] = mapped_column(Text)
    answers: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    takenAt: Mapped[str | None] = mapped_column(String, nullable=True)


class Job(Base):
    __tablename__ = "Job"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    recruiterId: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String)
    company: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    district: Mapped[str] = mapped_column(String)
    salaryMin: Mapped[int] = mapped_column(Integer)
    salaryMax: Mapped[int] = mapped_column(Integer)
    requiredSkills: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    postedAt: Mapped[str | None] = mapped_column(String, nullable=True)


class Match(Base):
    __tablename__ = "Match"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    traineeId: Mapped[str] = mapped_column(String)
    jobId: Mapped[str] = mapped_column(String)
    score: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="suggested")


class ChatMessage(Base):
    __tablename__ = "ChatMessage"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    profileId: Mapped[str | None] = mapped_column(String, nullable=True)
    traineeId: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    createdAt: Mapped[str | None] = mapped_column(String, nullable=True)


# ---------------------------------------------------------------------------
# Auth — same contract as the Express middleware
# ---------------------------------------------------------------------------
jwks_cache: tuple[float, list] | None = None


async def get_jwks() -> list | None:
    global jwks_cache
    if jwks_cache and time.time() - jwks_cache[0] < 3600:
        return jwks_cache[1]
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json")
            if r.status_code == 200:
                keys = r.json()["keys"]
                jwks_cache = (time.time(), keys)
                return keys
    except Exception:
        return None
    return None


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


async def verify_supabase_jwt(token: str) -> dict | None:
    try:
        header = json.loads(b64url_decode(token.split(".")[0]))
        keys = await get_jwks()
        if not keys:
            return None
        from jwt import PyJWK
        for k in keys:
            if k.get("kid") != header.get("kid"):
                continue
            pub = PyJWK.from_dict(k).key
            payload = jwt.decode(token, pub, algorithms=["RS256"])
            return {"email": payload.get("email", ""),
                    "name": (payload.get("user_metadata") or {}).get("full_name")}
    except Exception:
        return None
    return None


async def current_profile(request: Request, db: Session = Depends(lambda: None)) -> Profile | None:
    """x-demo-token header OR Supabase Bearer JWT -> Profile (None = spectator)."""
    demo = request.headers.get("x-demo-token")
    if demo:
        p = db.get(Profile, demo)
        if p:
            return p
    bearer = (request.headers.get("authorization") or "").replace("Bearer ", "")
    if bearer and SUPABASE_URL:
        claims = await verify_supabase_jwt(bearer)
        if claims and claims["email"]:
            return db.execute(select(Profile).where(Profile.email == claims["email"])) \
                .scalars().first()
    return None


def need_profile(request: Request) -> Profile:
    p = getattr(request.state, "profile", None)
    if not p:
        raise HTTPException(401, "Login required")
    return p


def need_role(request: Request, *roles: str) -> Profile:
    p = need_profile(request)
    if p.role not in roles:
        raise HTTPException(403, "Forbidden for your role")
    return p


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # resolve profile once per request (same as Express authMiddleware)
    with SessionLocal() as db:
        request.state.profile = await current_profile(request, db)
    return await call_next(request)


def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Domain logic (plain functions — the architectural payoff of unification)
# ---------------------------------------------------------------------------
def readiness_of(db: Session, trainee: Trainee) -> int:
    """Weighted level-fit readiness — same math as readinessFrom() in trainee.ts."""
    if not trainee.targetJobId:
        return 0
    tj = db.get(TargetJob, trainee.targetJobId)
    if not tj:
        return 0
    reqs = J(tj.requiredSkills) or []
    if not reqs:
        return 0
    levels = {ts.skillId: ts.level for ts in db.execute(
        select(TraineeSkill).where(TraineeSkill.traineeId == trainee.id)).scalars()}
    total = sum(r["weight"] for r in reqs)
    if total <= 0:
        return 0
    s = sum(min(100, round(100 * levels.get(r["skillId"], 0) / max(1, r["minLevel"]))) * r["weight"]
            for r in reqs)
    return round(s / total)


def score_job(db: Session, trainee: Trainee, job: Job) -> dict:
    """Weighted skill fit + location fit — same math as lib/match.ts."""
    reqs = J(job.requiredSkills) or []
    levels = {ts.skillId: ts.level for ts in db.execute(
        select(TraineeSkill).where(TraineeSkill.traineeId == trainee.id)).scalars()}
    names = {s.id: s.name for s in db.execute(select(Skill)).scalars()}
    skills, wsum, wtot = [], 0, 0
    for r in reqs:
        level = levels.get(r["skillId"], 0)
        fit = max(0, min(100, round(100 * level / max(1, r["minLevel"]))))
        wsum += fit * r["weight"]
        wtot += r["weight"]
        skills.append({"skillId": r["skillId"], "name": names.get(r["skillId"], "(skill)"),
                       "weight": r["weight"], "traineeLevel": level,
                       "minLevel": r["minLevel"], "met": level >= r["minLevel"]})
    skill_fit = round(wsum / wtot) if wtot else 50
    loc_fit = 100 if job.state == trainee.state and job.district == trainee.district \
        else 85 if job.state == trainee.state else 45
    skills.sort(key=lambda s: -s["weight"])
    return {"score": round(skill_fit * 0.8 + loc_fit * 0.2), "skillFit": skill_fit,
            "locFit": loc_fit, "skills": skills,
            "gaps": [s for s in skills if not s["met"]]}


def trainee_for(db: Session, profile: Profile) -> Trainee | None:
    return db.execute(select(Trainee).where(Trainee.profileId == profile.id)) \
        .scalars().first()


# ---------------------------------------------------------------------------
# Inline AI (fallbacks ported from apps/ai — same outputs, no HTTP hop)
# ---------------------------------------------------------------------------
def quiz_fallback(skill: str) -> list[dict]:
    return [
        {"q": f"Which best describes your experience with {skill}?",
         "options": ["None", "Basic tutorials", "Regular projects", "Professional work"], "answerIdx": 0},
        {"q": f"A junior asks you to explain {skill}. What's truest?",
         "options": ["Can't yet", "Simple ideas only", "Most topics", "Could teach it"], "answerIdx": 0},
        {"q": f"How do you handle a hard, unfamiliar {skill} problem?",
         "options": ["Stuck", "Search similar", "Decompose + solve", "Mentor others on it"], "answerIdx": 1},
        {"q": f"How often do you practice {skill}?",
         "options": ["Rarely", "Weekly", "Few times a week", "Daily"], "answerIdx": 1},
        {"q": f"Could you use {skill} in a job tomorrow?",
         "options": ["No", "With guidance", "Independently", "And review others' work"], "answerIdx": 1},
        {"q": f"Which shows real {skill} mastery?",
         "options": ["Watched tutorials", "Finished a course", "Built projects", "Shipped production work"], "answerIdx": 3},
    ]


async def ai_generate_quiz(skill: str) -> tuple[list[dict] | None, str]:
    if not os.getenv("AI_API_KEY"):
        return quiz_fallback(skill), "fallback"
    try:
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post("https://api.openai.com/v1/chat/completions",
                             headers={"Authorization": f"Bearer {os.getenv('AI_API_KEY')}"},
                             json={"model": os.getenv("AI_MODEL", "gpt-4o-mini"),
                                   "messages": [
                                       {"role": "system", "content":
                                        'You write MCQ assessments. 6 questions, 4 options, one correct. '
                                        'Reply JSON: {"questions": [{"q","options":[4],"answerIdx"}]}'},
                                       {"role": "user", "content": f"Skill: {skill}. Level: mixed."}],
                                   "response_format": {"type": "json_object"}})
            data = r.json()
            qs = json.loads(data["choices"][0]["message"]["content"])["questions"]
            if len(qs) >= 4:
                return qs[:6], "ai"
    except Exception:
        pass
    return quiz_fallback(skill), "fallback"


def agent_reply_fallback(message: str, profile: dict) -> str:
    m = message.lower()
    tr = profile.get("trainee") or {}
    if any(k in m for k in ["this week", "roadmap", "study", "next", "what should i do"]):
        nxt = tr.get("nextRoadmapItem") or {}
        return nxt.get("advice", "Open the Roadmap page and start the first pending item.")
    if any(k in m for k in ["job", "apply", "match", "hiring"]):
        matches = tr.get("recentMatches") or []
        if matches:
            top = matches[0]
            return (f"Your best match right now: {top['title']} at {top['company']} "
                    f"({top['score']}% fit, ₹{top['salaryMin']}-{top['salaryMax']}/mo in {top['district']}). "
                    "Open Jobs to apply.")
        return "No scored matches yet — complete your Skill Analysis first, then check Jobs."
    if any(k in m for k in ["ready", "hireable", "reality"]):
        r = tr.get("readiness")
        return (f"Your readiness is {r}%. "
                if r is not None else "Complete onboarding to see your readiness. ") + \
               "Check Skill Analysis for the exact gaps."
    return ("I'm the SkilloMetrics counsellor (unified-backend fallback mode). Try: "
            '"What should I do this week?" or "Show my best job matches."')


async def ai_agent_chat(message: str, profile: dict) -> tuple[str, str]:
    if not os.getenv("AI_API_KEY"):
        return agent_reply_fallback(message, profile), "fallback"
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post("https://api.openai.com/v1/chat/completions",
                             headers={"Authorization": f"Bearer {os.getenv('AI_API_KEY')}"},
                             json={"model": os.getenv("AI_MODEL", "gpt-4o-mini"),
                                   "messages": [
                                       {"role": "system", "content":
                                        "You are SkilloMetrics' career counsellor for Indian skilling "
                                        "trainees. Use the provided profile JSON (skills, readiness, "
                                        "matches, roadmap) to answer concretely."},
                                       {"role": "user", "content": f"Profile: {json.dumps(profile)[:2000]}\n\nMessage: {message}"}],
                                   "response_format": {"type": "text"}})
            return r.json()["choices"][0]["message"]["content"], "ai"
    except Exception:
        return agent_reply_fallback(message, profile), "fallback"


# ---------------------------------------------------------------------------
# Routes — the demo path
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"ok": True, "service": "unified-api", "aiMode": AI_MODE}


@app.get("/api/auth/oauth-configured")
async def oauth_configured():
    return {"configured": bool(SUPABASE_URL), "providers": ["google", "github", "linkedin"],
            "note": "unified-backend prototype"}


@app.get("/api/auth/personas")
async def personas(db: Session = Depends(db_session)):
    rows = db.execute(select(Profile).where(Profile.email.contains("demo")).limit(8)).scalars()
    return [{"id": p.id, "email": p.email, "name": p.name, "role": p.role} for p in rows]


@app.post("/api/auth/demo-login")
async def demo_login(body: dict, db: Session = Depends(db_session)):
    email = (body or {}).get("email")
    if not email:
        raise HTTPException(400, "email required")
    p = db.execute(select(Profile).where(Profile.email == email)).scalars().first()
    if not p:
        raise HTTPException(404, "No profile for that email")
    return {"id": p.id, "email": p.email, "name": p.name, "role": p.role}


@app.get("/api/auth/me")
async def me(request: Request, db: Session = Depends(db_session)):
    profile = need_profile(request)
    t = trainee_for(db, profile) if profile.role == "trainee" else None
    return {"profile": {"id": profile.id, "email": profile.email, "name": profile.name,
                        "role": profile.role},
            "trainee": {"id": t.id, "name": t.name, "state": t.state,
                        "district": t.district, "weeklyHours": t.weeklyHours} if t else None}


@app.get("/api/skill-analysis")
async def skill_analysis(request: Request, db: Session = Depends(db_session)):
    profile = need_profile(request)
    t = trainee_for(db, profile)
    if not t:
        raise HTTPException(404, "Complete onboarding first")
    if not t.targetJobId:
        raise HTTPException(400, "No target job set")
    tj = db.get(TargetJob, t.targetJobId)
    reqs = J(tj.requiredSkills) or []
    levels = {ts.skillId: ts.level for ts in db.execute(
        select(TraineeSkill).where(TraineeSkill.traineeId == t.id)).scalars()}

    skill_rows = []
    for r in reqs:
        skill = db.get(Skill, r["skillId"])
        level = levels.get(r["skillId"], 0)
        resources = db.execute(select(LearningResource)
                               .where(LearningResource.skillId == r["skillId"])).scalars()
        skill_rows.append({
            "skillId": r["skillId"], "name": skill.name if skill else "?",
            "category": skill.category if skill else "?", "level": level,
            "minLevel": r["minLevel"], "weight": r["weight"],
            "gap": max(0, r["minLevel"] - level), "met": level >= r["minLevel"],
            "resources": [{"id": x.id, "title": x.title, "platform": x.platform,
                           "url": x.url, "language": x.language, "cost": x.cost,
                           "durationHours": x.durationHours, "rating": x.rating}
                          for x in resources]})

    jobs = db.execute(select(Job).where(Job.active,
                                        Job.title.contains(tj.title.split(" ")[0]))).scalars().all()
    benchmark = []
    for s in skill_rows:
        demand = sum(1 for j in jobs if any(x["skillId"] == s["skillId"]
                                            for x in (J(j.requiredSkills) or [])))
        benchmark.append({"skillId": s["skillId"], "name": s["name"],
                          "demandPct": round(100 * demand / len(jobs)) if jobs else 0,
                          "traineeLevel": s["level"]})

    readiness = readiness_of(db, t)
    missing = sorted((s for s in skill_rows if not s["met"]), key=lambda s: -s["gap"])
    gap_hours = sum(min((r["durationHours"] for r in s["resources"] if r["cost"] == "free"),
                        default=12) for s in missing)
    weeks = max(1, -(-gap_hours // max(1, t.weeklyHours)))
    return {
        "targetJob": {"id": tj.id, "title": tj.title, "family": tj.family},
        "readiness": readiness,
        "hireableToday": readiness >= 70 and not missing,
        "skills": skill_rows, "benchmark": benchmark,
        "realityCheck": {
            "requirementsMet": len(skill_rows) - len(missing),
            "requirementsTotal": len(skill_rows),
            "biggestGaps": [s["name"] for s in missing[:3]],
            "totalGapHours": gap_hours, "weeklyHours": t.weeklyHours,
            "estimatedWeeks": weeks,
            "etaDate": (datetime.now(timezone.utc) + timedelta(weeks=weeks)).isoformat(),
            "jobsAnalyzed": len(jobs)},
        "source": "computed",
    }


@app.get("/api/roadmap")
async def get_roadmap(request: Request, db: Session = Depends(db_session)):
    profile = need_profile(request)
    t = trainee_for(db, profile)
    if not t:
        raise HTTPException(404, "No trainee")
    items = db.execute(select(RoadmapItem).where(RoadmapItem.traineeId == t.id)
                       .order_by(RoadmapItem.order)).scalars().all()
    out = []
    for it in items:
        skill = db.get(Skill, it.skillId)
        res = db.get(LearningResource, it.resourceId) if it.resourceId else None
        out.append({"id": it.id, "status": it.status, "estHours": it.estHours,
                    "targetWeeks": it.targetWeeks, "order": it.order,
                    "skill": {"id": skill.id, "name": skill.name} if skill else None,
                    "resource": {"id": res.id, "title": res.title, "platform": res.platform,
                                 "url": res.url, "cost": res.cost} if res else None})
    return {"items": out, "weeklyHours": t.weeklyHours}


@app.post("/api/roadmap/build")
async def build_roadmap(request: Request, db: Session = Depends(db_session)):
    profile = need_profile(request)
    t = trainee_for(db, profile)
    if not t or not t.targetJobId:
        raise HTTPException(404, "No trainee / target job")
    tj = db.get(TargetJob, t.targetJobId)
    reqs = J(tj.requiredSkills) or []
    levels = {ts.skillId: ts.level for ts in db.execute(
        select(TraineeSkill).where(TraineeSkill.traineeId == t.id)).scalars()}
    gaps = sorted((r for r in reqs if levels.get(r["skillId"], 0) < r["minLevel"]),
                  key=lambda r: -(r["weight"] * (r["minLevel"] - levels.get(r["skillId"], 0))))

    # clear pending, keep done
    for it in db.execute(select(RoadmapItem).where(
            RoadmapItem.traineeId == t.id, RoadmapItem.status != "done")).scalars():
        db.delete(it)
    order = len(list(db.execute(select(RoadmapItem)
                                .where(RoadmapItem.traineeId == t.id)).scalars()))
    cursor = datetime.now(timezone.utc)
    for g in gaps:
        resources = db.execute(select(LearningResource).where(
            LearningResource.skillId == g["skillId"])).scalars().all()
        pick = min(resources, key=lambda r: (0 if r.cost == "free" else 1 if r.cost == "freemium" else 2,
                                             -r.rating), default=None)
        est = min(pick.durationHours, max(6, g["minLevel"] - g.get("level", 0))) if pick \
            else max(6, g["minLevel"] - g.get("level", 0))
        weeks = max(1, -(-int(est) // max(1, t.weeklyHours)))
        cursor += timedelta(weeks=weeks)
        db.add(RoadmapItem(id=f"u{int(time.time()*1000)}{order}", traineeId=t.id,
                           skillId=g["skillId"], resourceId=pick.id if pick else None,
                           estHours=est, weeklyHours=t.weeklyHours, targetWeeks=weeks,
                           status="pending", order=order))
        order += 1
    db.commit()
    return await get_roadmap(request, db)


@app.post("/api/assessments/start")
async def start_assessment(request: Request, body: dict, db: Session = Depends(db_session)):
    profile = need_profile(request)
    t = trainee_for(db, profile)
    if not t:
        raise HTTPException(404, "No trainee")
    skill = db.get(Skill, (body or {}).get("skillId"))
    if not skill:
        raise HTTPException(404, "Skill not found")
    questions, source = await ai_generate_quiz(skill.name)
    aid = f"a{int(time.time()*1000)}{t.id[:6]}"
    db.add(Assessment(id=aid, traineeId=t.id, skillId=skill.id, questions=JS(questions),
                      takenAt=now_iso()))
    db.commit()
    return {"id": aid, "skill": {"id": skill.id, "name": skill.name},
            "questions": [{"q": q["q"], "options": q["options"]} for q in questions],
            "source": source}


@app.post("/api/assessments/{aid}/submit")
async def submit_assessment(request: Request, aid: str, body: dict,
                            db: Session = Depends(db_session)):
    profile = need_profile(request)
    t = trainee_for(db, profile)
    if not t:
        raise HTTPException(404, "No trainee")
    a = db.get(Assessment, aid)
    if not a or a.traineeId != t.id:
        raise HTTPException(404, "Not found")
    questions = J(a.questions) or []
    answers = (body or {}).get("answers") or []
    if len(answers) != len(questions):
        raise HTTPException(400, f"Expected {len(questions)} answers")
    correct = sum(1 for q, ans in zip(questions, answers) if ans == q["answerIdx"])
    score = round(100 * correct / len(questions))
    a.answers = JS(answers)
    a.score = score
    existing = db.execute(select(TraineeSkill).where(
        TraineeSkill.traineeId == t.id, TraineeSkill.skillId == a.skillId)).scalars().first()
    if existing:
        existing.level = max(existing.level, score)
        existing.source = "assessment"
    else:
        db.add(TraineeSkill(id=f"ts{int(time.time()*1000)}", traineeId=t.id,
                            skillId=a.skillId, level=score, source="assessment",
                            updatedAt=now_iso()))
    db.commit()
    return {"score": score, "correct": correct, "total": len(questions)}


@app.get("/api/jobs/matches")
async def job_matches(request: Request, db: Session = Depends(db_session)):
    profile = need_profile(request)
    t = trainee_for(db, profile)
    if not t:
        raise HTTPException(404, "No trainee")
    jobs = db.execute(select(Job).where(Job.active)).scalars().all()
    scored = []
    for j in jobs:
        breakdown = score_job(db, t, j)
        m = db.execute(select(Match).where(Match.traineeId == t.id,
                                           Match.jobId == j.id)).scalars().first()
        scored.append({"job": {"id": j.id, "title": j.title, "company": j.company,
                               "state": j.state, "district": j.district,
                               "salaryMin": j.salaryMin, "salaryMax": j.salaryMax},
                       "match": {"id": m.id, "status": m.status} if m else None,
                       "breakdown": breakdown})
    scored.sort(key=lambda s: -s["breakdown"]["score"])
    return scored


@app.get("/api/market/insights")
async def market_insights(request: Request, db: Session = Depends(db_session)):
    profile = need_profile(request)
    t = trainee_for(db, profile)
    if not t:
        raise HTTPException(404, "No trainee")
    tj = db.get(TargetJob, t.targetJobId) if t.targetJobId else None
    role_key = (tj.title.split(" ")[0].lower() if tj else "")
    jobs = db.execute(select(Job).where(Job.active,
                                        Job.title.contains(role_key))).scalars().all()
    state_jobs = [j for j in jobs if j.state == t.state]

    def band(js):
        if not js:
            return None
        mids = [round((j.salaryMin + j.salaryMax) / 2) for j in js]
        return {"min": min(j.salaryMin for j in js), "max": max(j.salaryMax for j in js),
                "avg": round(sum(mids) / len(mids)), "count": len(js)}

    demand: dict[str, int] = {}
    names = {s.id: s.name for s in db.execute(select(Skill)).scalars()}
    for j in jobs:
        for r in (J(j.requiredSkills) or []):
            n = names.get(r["skillId"])
            if n:
                demand[n] = demand.get(n, 0) + 1
    top = sorted(({"skill": k, "pct": round(100 * v / len(jobs)) if jobs else 0}
                  for k, v in demand.items()), key=lambda d: -d["pct"])[:8]
    return {"role": tj.title if tj else "", "state": t.state,
            "national": band(jobs), "stateBand": band(state_jobs), "topSkills": top}


@app.post("/api/chat")
async def chat(request: Request, body: dict, db: Session = Depends(db_session)):
    profile = need_profile(request)
    t = trainee_for(db, profile) if profile.role == "trainee" else None
    message = (body or {}).get("message")
    if not message:
        raise HTTPException(400, "message required")

    # THE payoff of unification: agent context is a function call away —
    # the Express version needs ~50 lines assembling this over HTTP.
    agent_ctx: dict = {"role": profile.role, "name": profile.name}
    if t:
        jobs = db.execute(select(Job).where(Job.active)).scalars().all()
        scored = sorted(((j, score_job(db, t, j)) for j in jobs),
                        key=lambda p: -p[1]["score"])[:4]
        next_item = db.execute(select(RoadmapItem).where(
            RoadmapItem.traineeId == t.id, RoadmapItem.status != "done")
            .order_by(RoadmapItem.order)).scalars().first()
        agent_ctx["trainee"] = {
            "state": t.state, "district": t.district,
            "targetJob": db.get(TargetJob, t.targetJobId).title if t.targetJobId else None,
            "readiness": readiness_of(db, t),
            "recentMatches": [{"title": j.title, "company": j.company, "district": j.district,
                               "salaryMin": j.salaryMin, "salaryMax": j.salaryMax,
                               "score": b["score"]} for j, b in scored],
            "nextRoadmapItem": {"advice": f"Continue roadmap item {next_item.skillId} — "
                                          f"then take its assessment."} if next_item
            else {"advice": "All roadmap items done — apply to jobs now."},
        }

    db.add(ChatMessage(id=f"m{int(time.time()*1000)}", traineeId=t.id if t else None,
                       profileId=None if t else profile.id, role="user", content=message,
                       createdAt=now_iso()))
    db.commit()
    reply, source = await ai_agent_chat(message, agent_ctx)
    db.add(ChatMessage(id=f"m{int(time.time()*1000)}r", traineeId=t.id if t else None,
                       profileId=None if t else profile.id, role="agent", content=reply,
                       createdAt=now_iso()))
    db.commit()
    return {"reply": reply, "source": source}


# ---------------------------------------------------------------------------
# Browser console — self-contained page for exploring the API
# ---------------------------------------------------------------------------
from pathlib import Path
CONSOLE_DIR = Path(__file__).resolve().parent


@app.get("/console", response_class=HTMLResponse)
async def console_page():
    return HTMLResponse((CONSOLE_DIR / "console.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)
