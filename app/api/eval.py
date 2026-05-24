from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.models.user import User
from app.utils.security import get_current_user

router = APIRouter(prefix="/api/v1/eval", tags=["eval"])


class GoldenSample(BaseModel):
    query: str
    relevant_chunk_ids: list[str]
    user_id: str


class EvalResultOut(BaseModel):
    recall_at_5: float
    recall_at_10: float
    mrr: float
    ndcg_at_10: float
    total_queries: int


_last_result: dict | None = None


@router.post("/run", response_model=EvalResultOut)
async def run_eval(user: User = Depends(get_current_user)):
    """Run RAG evaluation against golden dataset."""
    from app.services.evaluator import evaluate_search

    result = await evaluate_search()
    if result.total_queries == 0:
        raise HTTPException(400, "No golden dataset found. Upload samples first.")

    global _last_result
    _last_result = {
        "recall_at_5": result.recall_at_5,
        "recall_at_10": result.recall_at_10,
        "mrr": result.mrr,
        "ndcg_at_10": result.ndcg_at_10,
        "total_queries": result.total_queries,
        "details": result.details,
    }

    return EvalResultOut(
        recall_at_5=result.recall_at_5,
        recall_at_10=result.recall_at_10,
        mrr=result.mrr,
        ndcg_at_10=result.ndcg_at_10,
        total_queries=result.total_queries,
    )


@router.get("/results")
async def get_results(user: User = Depends(get_current_user)):
    """Get last evaluation results."""
    if _last_result is None:
        raise HTTPException(404, "No evaluation results yet. Run /eval first.")
    return _last_result


@router.post("/samples")
async def add_samples(
    samples: list[GoldenSample],
    user: User = Depends(get_current_user),
):
    """Add samples to golden dataset."""
    from app.services.evaluator import load_golden_dataset, save_golden_dataset

    existing = load_golden_dataset()
    for s in samples:
        existing.append(s.model_dump())
    save_golden_dataset(existing)
    return {"status": "added", "total_samples": len(existing)}
