# -*- coding: utf-8 -*-
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional
import sqlite3
import os
import json
import requests
import re
import uvicorn

# --- 설정 --- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 상위 폴더의 DB 및 데이터셋 참조
PARENT_DB_PATH = os.path.abspath(os.path.join(BASE_DIR, "..", "incident_reports.db"))
PARENT_DATASET_PATH = os.path.abspath(os.path.join(BASE_DIR, "..", "dataset_from_data_txt.json"))

SQLITE_DB_PATH = PARENT_DB_PATH
DATASET_PATH = PARENT_DATASET_PATH

# 로컬 파일 우선 확인
if os.path.exists(os.path.join(BASE_DIR, "incident_reports.db")):
    SQLITE_DB_PATH = os.path.join(BASE_DIR, "incident_reports.db")
if os.path.exists(os.path.join(BASE_DIR, "dataset_from_data_txt.json")):
    DATASET_PATH = os.path.join(BASE_DIR, "dataset_from_data_txt.json")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip().rstrip("/")
ANALYSIS_LLM_MODEL = "hf.co/unsloth/gemma-3n-E2B-it-GGUF:Q4_K_M"

app = FastAPI(title="Independent Analyzer", description="독립 실행형 장애 분석기")

# --- Pydantic 모델 --- #
class AnalysisRequest(BaseModel):
    fault_type: str

class CauseActionItem(BaseModel):
    text: str
    count: int

class AnalysisResponse(BaseModel):
    fault_type: str
    total_incidents: int
    importance_level: str
    yearly_frequency: float
    top_causes: List[CauseActionItem]
    top_actions: List[CauseActionItem]
    ai_recommendation: str
    mode: str

# --- 분석 로직 --- #
@app.post("/analyze", response_model=AnalysisResponse)
async def analyze_fault(request: AnalysisRequest):
    conn = None
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cursor = conn.cursor()
        
        # 1. 통계 수집
        cursor.execute("SELECT COUNT(*) FROM incident_data WHERE `장애명` = ?", (request.fault_type,))
        total_incidents = cursor.fetchone()[0]
        
        cursor.execute("SELECT MIN(`장애일시`), MAX(`장애일시`) FROM incident_data WHERE `장애명` = ?", (request.fault_type,))
        min_date, max_date = cursor.fetchone()
        
        yearly_frequency = 0
        if total_incidents > 0 and min_date and max_date:
            try:
                min_year = int(min_date.split('-')[0])
                max_year = int(max_date.split('-')[0])
                span = max(1, max_year - min_year + 1)
                yearly_frequency = total_incidents / span
            except:
                yearly_frequency = float(total_incidents)
        
        importance_level = "정보없음"
        if yearly_frequency >= 10: importance_level = "높음"
        elif yearly_frequency >= 5: importance_level = "중간"
        elif total_incidents > 0: importance_level = "낮음"

        # 2. 원인 및 조치 집계
        cursor.execute("SELECT `장애 원인`, COUNT(*) as cnt FROM incident_data WHERE `장애명` = ? AND `장애 원인` IS NOT NULL GROUP BY `장애 원인` ORDER BY cnt DESC LIMIT 5", (request.fault_type,))
        top_causes = [CauseActionItem(text=row[0], count=row[1]) for row in cursor.fetchall()]
        
        cursor.execute("SELECT `장애 발생 시 조치 방법`, COUNT(*) as cnt FROM incident_data WHERE `장애명` = ? AND `장애 발생 시 조치 방법` IS NOT NULL GROUP BY `장애 발생 시 조치 방법` ORDER BY cnt DESC LIMIT 5", (request.fault_type,))
        top_actions = [CauseActionItem(text=row[0], count=row[1]) for row in cursor.fetchall()]
        
        # 3. LLM 분석
        ai_recommendation = ""
        mode = "Basic"
        
        # Ollama 체크
        ollama_available = False
        try:
            requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=1)
            ollama_available = True
        except:
            pass

        if ollama_available and top_causes:
            mode = "LLM_Enhanced"
            causes_text = "\n".join([f"- {c.text} ({c.count}건)" for c in top_causes[:3]])
            actions_text = "\n".join([f"- {a.text} ({a.count}건)" for a in top_actions[:3]])
            
            # RAG: 데이터셋 로드
            qa_text = ""
            if os.path.exists(DATASET_PATH):
                try:
                    with open(DATASET_PATH, 'r', encoding='utf-8') as f:
                        dataset = json.load(f)
                        # 간단한 키워드 매칭
                        related = [item for item in dataset if request.fault_type in (item.get('instruction','') + item.get('output',''))]
                        snippets = []
                        for item in related[:3]: # 상위 3개만
                            q = item.get('instruction','').strip()
                            a = item.get('output','').strip()
                            snippets.append(f"Q: {q}\nA: {a}")
                        qa_text = "\n\n".join(snippets)
                except Exception as e:
                    print(f"데이터셋 로드 오류: {e}")

            prompt = f"""
{request.fault_type} 장애에 대해 다음 3가지 항목으로 상세히 분석 보고서를 작성해줘.
1. 원인 분석
2. 예방 조치
3. 결론

참고 데이터:
[주요 원인]
{causes_text}

[주요 조치]
{actions_text}

[관련 지식(Q&A)]
{qa_text}

각 항목은 구체적이고 전문적으로 작성하고, 한국어로 출력해줘.
"""
            try:
                resp = requests.post(
                    f"{OLLAMA_BASE_URL}/api/generate",
                    json={
                        "model": ANALYSIS_LLM_MODEL,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.7}
                    },
                    timeout=120
                )
                if resp.status_code == 200:
                    ai_recommendation = resp.json().get("response", "")
                else:
                    ai_recommendation = f"LLM 오류: {resp.status_code}"
            except Exception as e:
                ai_recommendation = f"LLM 호출 실패: {e}"
        else:
            ai_recommendation = "LLM을 사용할 수 없거나 데이터가 부족하여 기본 통계만 제공합니다."

        return AnalysisResponse(
            fault_type=request.fault_type,
            total_incidents=total_incidents,
            importance_level=importance_level,
            yearly_frequency=yearly_frequency,
            top_causes=top_causes,
            top_actions=top_actions,
            ai_recommendation=ai_recommendation,
            mode=mode
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn: conn.close()

@app.get("/fault_types")
async def get_fault_types():
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT 장애명 FROM incident_data WHERE 장애명 IS NOT NULL AND 장애명 != ''")
        types = [row[0] for row in cursor.fetchall()]
        return {"fault_types": sorted(types)}
    finally:
        conn.close()

# --- UI 서빙 --- #
# 정적 파일 서빙 (로고 이미지 등)
app.mount("/static", StaticFiles(directory=BASE_DIR), name="static")

@app.get("/io.png")
async def get_logo():
    logo_path = os.path.join(BASE_DIR, "io.png")
    if os.path.exists(logo_path):
        return FileResponse(logo_path)
    raise HTTPException(status_code=404, detail="Logo not found")

@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open(os.path.join(BASE_DIR, "index.html"), "r", encoding="utf-8") as f:
        return f.read()

if __name__ == "__main__":
    print(f"독립 분석기 시작... (DB: {SQLITE_DB_PATH}, Dataset: {DATASET_PATH})")
    uvicorn.run(app, host="0.0.0.0", port=8002)
