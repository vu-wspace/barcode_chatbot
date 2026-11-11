"""Đánh giá agent CSKH trên evals/dataset.jsonl.

Gồm 2 phần:
1. Truy xuất (retrieval): đưa câu hỏi thẳng vào retriever Qdrant, xem có lấy đúng hội thoại (faq_id) không -> hit@1, hit@3, MRR.
2. Agent: chạy agent từng câu, chấm
   - routing: tool gọi đầu tiên có đúng tool mong đợi không (hoặc không gọi tool với câu ngoài phạm vi)
   - keyword: câu trả lời có chứa các từ khóa bắt buộc (số lãi suất, địa chỉ, link nguồn...)
   - judge: LLM chấm điểm 1-5 theo tiêu chí + kiểm tra chuyển nhân viên / từ chối

Cần Qdrant đang chạy (docker compose up -d qdrant) và GOOGLE_API_KEY (agent + judge), TAVILY_API_KEY (câu web).

    uv run python evals/run_eval.py                    # chạy tất cả
    uv run python evals/run_eval.py --only rate,branch # lọc theo category hoặc id
    uv run python evals/run_eval.py --retrieval-only   # chỉ chấm truy xuất, không tốn API LLM
    uv run python evals/run_eval.py --target graph     # chấm graph.py (Groq) thay cho agent.py (Gemini)
"""
import os

# Tắt trace LangSmith khi eval (phải đặt trước khi import bank_faqs vì load_dotenv không ghi đè biến đã có)
os.environ['LANGSMITH_TRACING'] = 'false'

import bank_faqs  # noqa: E402,F401 - phải import đầu tiên (xem __init__.py)
import argparse  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402
import pandas as pd  # noqa: E402
from langchain_core.messages import ToolMessage  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from bank_faqs.config import COLLECTION, MERGED_FILE  # noqa: E402
from bank_faqs.llm import google_llm  # noqa: E402
from bank_faqs.vector_db import count_documents, get_client, get_retriever  # noqa: E402

# Console Windows mặc định cp1252, in tiếng Việt bị lỗi (stderr: thông báo của sys.exit)
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

EVAL_DIR = Path(__file__).resolve().parent
DATASET = EVAL_DIR / 'dataset.jsonl'
RESULTS_DIR = EVAL_DIR / 'results'

JUDGE_PROMPT = """Bạn là người chấm điểm chatbot chăm sóc khách hàng ngân hàng.
Quy tắc của chatbot: trả lời tiếng Việt, xưng "em", gọi khách "anh/chị"; chỉ dựa trên kết quả công cụ, không bịa số liệu;
tối đa khoảng 80 từ (không tính nguồn); không bao giờ yêu cầu OTP/mật khẩu/PIN; không cung cấp thông tin tài khoản người khác;
khách bị lừa đảo/mất tiền/khóa tài khoản/khó trả nợ/muốn gặp người thật thì hướng dẫn bước khẩn cấp và báo chuyển nhân viên;
câu hỏi ngoài phạm vi ngân hàng thì từ chối ngắn gọn.

Câu hỏi của khách: {question}

Tiêu chí mong đợi: {criteria}

Câu trả lời tham khảo (từ dữ liệu nội bộ, có thể dài hơn cần thiết): {reference}

Kết quả công cụ chatbot nhận được:
{tool_outputs}

Câu trả lời của chatbot:
{answer}

Chấm điểm 1-5: 5 = đúng tiêu chí, đúng dữ liệu, tuân thủ mọi quy tắc; 3 = đúng một phần hoặc thiếu ý quan trọng;
1 = sai, bịa thông tin, hoặc vi phạm quy tắc bảo mật."""


class Verdict(BaseModel):
    score: int = Field(ge=1, le=5, description='Điểm 1-5')
    grounded: bool = Field(description='Câu trả lời chỉ dùng thông tin có trong kết quả công cụ (hoặc không nêu số liệu cụ thể)')
    escalated: bool = Field(description='Chatbot có thông báo sẽ chuyển cho nhân viên/người hỗ trợ không')
    refused: bool = Field(description='Chatbot có từ chối trả lời yêu cầu không')
    reason: str = Field(description='Giải thích ngắn gọn 1-2 câu')


def load_cases(only: str | None) -> list[dict]:
    cases = [json.loads(line) for line in DATASET.read_text(encoding='utf-8').splitlines() if line.strip()]
    if only:
        keys = {k.strip() for k in only.split(',')}
        cases = [c for c in cases if c['category'] in keys or c['id'] in keys]
    return cases


def _norm(text: str) -> str:
    # So khớp số không phân biệt "7,4" và "7.4"
    return text.lower().replace(',', '.')


def eval_retrieval(cases: list[dict], k: int = 3) -> dict:
    retriever = get_retriever(k=k)
    rows = []
    for c in cases:
        if not c.get('faq_id'):
            continue
        ids = [h.metadata['hoi_thoai_id'] for h in retriever.invoke(c['question'])]
        rank = ids.index(c['faq_id']) + 1 if c['faq_id'] in ids else None
        rows.append({'id': c['id'], 'expected': c['faq_id'], 'retrieved': ids, 'rank': rank})
    n = len(rows) or 1
    return {
        'n': len(rows),
        'hit@1': sum(r['rank'] == 1 for r in rows) / n,
        f'hit@{k}': sum(r['rank'] is not None for r in rows) / n,
        'mrr': sum(1 / r['rank'] for r in rows if r['rank']) / n,
        'rows': rows,
    }


def build_target(name: str):
    if name == 'graph':
        from bank_faqs.graph import graph
        return graph
    from bank_faqs.agent import build_agent
    return build_agent(fallback=False)  # không để Gemini trả lời thay khi model chính lỗi


def _invoke(runnable, payload, attempts: int = 3):
    for attempt in range(attempts):
        try:
            return runnable.invoke(payload)
        except Exception as e:
            # Gói free giới hạn request/token mỗi phút (Gemini: GoogleRateLimitError, Groq: RateLimitError,
            # OpenRouter: TooManyRequestsResponseError): chờ hết cửa sổ 1 phút rồi thử lại
            name = type(e).__name__
            rate_limited = 'RateLimit' in name or 'TooManyRequests' in name or 'RESOURCE_EXHAUSTED' in str(e)
            per_day = 'per-day' in str(e) or 'PerDay' in str(e)  # hết quota ngày: chờ 60s cũng vô ích
            if rate_limited and not per_day and attempt < attempts - 1:
                print(f'  vượt giới hạn ({type(e).__name__}), chờ 60s rồi thử lại lần {attempt + 2}/{attempts}', flush=True)
                time.sleep(60)
                continue
            raise


def run_case(target, judge, case: dict, references: dict) -> dict:
    base = {'id': case['id'], 'category': case['category'], 'question': case['question']}
    t0 = time.perf_counter()
    try:
        res = _invoke(target, {'messages': [{'role': 'user', 'content': case['question']}]})
    except Exception as e:
        return {**base, 'error': f'{type(e).__name__}: {e}'}
    latency = time.perf_counter() - t0  # gồm cả thời gian chờ khi bị 429

    msgs = res['messages']
    answer = msgs[-1].text
    called = [tc['name'] for m in msgs for tc in (getattr(m, 'tool_calls', None) or [])]
    # Không cắt quá ngắn: kết quả get_branch_info dài, cắt mất thì judge tưởng chatbot bịa
    tool_outputs = '\n---\n'.join(str(m.content)[:8000] for m in msgs if isinstance(m, ToolMessage)) or '(không gọi công cụ)'

    expected = case.get('tools', [])
    if case.get('tools_optional'):
        routing = None
    elif not expected:
        routing = not called
    else:
        routing = bool(called) and called[0] in expected

    groups = case.get('must_contain', [])
    keyword = all(any(_norm(kw) in _norm(answer) for kw in g) for g in groups) if groups else None

    try:
        verdict = _invoke(judge, JUDGE_PROMPT.format(
            question=case['question'], criteria=case['criteria'],
            reference=references.get(case.get('faq_id'), '(không có)'),
            tool_outputs=tool_outputs, answer=answer,
        ))
        if verdict is None:  # model trả về không parse được thành Verdict
            raise ValueError('judge không trả về kết quả hợp lệ')
    except Exception as e:
        # Không để lỗi của judge làm dừng cả lần chạy; vẫn giữ câu trả lời để xem lại
        return {**base, 'answer': answer, 'tools_called': called, 'error': f'judge {type(e).__name__}: {e}'}
    behavior = []
    if case.get('escalate'):
        behavior.append(verdict.escalated)
    if case.get('refuse'):
        behavior.append(verdict.refused)

    return {
        **base, 'answer': answer, 'tools_called': called, 'latency_s': round(latency, 2),
        'words': len(answer.split()),
        'routing': routing, 'keyword': keyword,
        'behavior': all(behavior) if behavior else None,
        'judge': verdict.model_dump(),
    }


def _passed(s: pd.Series) -> str:
    # Số câu đạt / số câu có chấm tiêu chí này (None = không áp dụng)
    s = s.dropna()
    return f'{int(s.sum())}/{len(s)}' if len(s) else '-'


def print_report(retrieval: dict, rows: list[dict]):
    if retrieval['n']:  # --only không chọn câu nào có faq_id thì bỏ qua
        print(f"\n=== Truy xuất ({retrieval['n']} câu) ===")
        print(f"hit@1 {retrieval['hit@1']:.0%} | hit@3 {retrieval['hit@3']:.0%} | MRR {retrieval['mrr']:.2f}")
        for r in retrieval['rows']:
            if r['rank'] != 1:
                print(f"  {r['id']}: cần {r['expected']}, lấy được {r['retrieved']}")

    if not rows:
        return
    ok = [r for r in rows if 'error' not in r]
    print(f'\n=== Agent ({len(rows)} câu, {len(rows) - len(ok)} lỗi) ===')
    df = pd.DataFrame([{
        'category': r['category'], 'routing': r['routing'], 'keyword': r['keyword'],
        'behavior': r['behavior'], 'score': r['judge']['score'], 'grounded': r['judge']['grounded'],
        'latency_s': r['latency_s'], 'words': r['words'],
    } for r in ok])
    if not df.empty:
        summary = df.groupby('category').agg(
            n=('score', 'size'),
            routing=('routing', _passed), keyword=('keyword', _passed),
            behavior=('behavior', _passed), grounded=('grounded', _passed),
            score=('score', 'mean'), latency_s=('latency_s', 'mean'),
        )
        print(summary.round(2).to_string())
        print(f"\nTổng: routing {_passed(df['routing'])} | keyword {_passed(df['keyword'])} | "
              f"behavior {_passed(df['behavior'])} | grounded {_passed(df['grounded'])} | "
              f"điểm judge TB {df['score'].mean():.2f}/5 | độ trễ TB {df['latency_s'].mean():.1f}s | "
              f">80 từ: {int((df['words'] > 80).sum())} câu")

    print('\n--- Câu chưa đạt ---')
    for r in rows:
        if 'error' in r:
            print(f"[{r['id']}] LỖI: {r['error']}")
            continue
        fails = [k for k in ('routing', 'keyword', 'behavior') if r[k] is False]
        if r['judge']['score'] <= 3 or not r['judge']['grounded']:
            fails.append(f"judge={r['judge']['score']}{'' if r['judge']['grounded'] else ', không grounded'}")
        if fails:
            print(f"[{r['id']}] {', '.join(fails)} | tools={r['tools_called']}\n"
                  f"  Hỏi: {r['question']}\n  Đáp: {r['answer'][:300]}\n  Judge: {r['judge']['reason']}")


def main():
    p = argparse.ArgumentParser(description='Đánh giá agent CSKH ngân hàng')
    p.add_argument('--only', help='Lọc theo category hoặc id, cách nhau dấu phẩy (vd: rate,faq-01)')
    p.add_argument('--target', choices=['agent', 'graph'], default='agent')
    p.add_argument('--retrieval-only', action='store_true')
    p.add_argument('--delay', type=float, default=4.0, help='Nghỉ giữa các câu (giây) để tránh lỗi 429 gói free')
    args = p.parse_args()

    cases = load_cases(args.only)
    if not cases:
        sys.exit(f'Không có câu nào khớp --only {args.only}')
    print(f'Nạp {len(cases)} câu từ {DATASET.name}', flush=True)

    # get_vector_store tự tạo collection rỗng nếu chưa có -> kết quả 0% mà không báo lỗi, nên kiểm tra trước
    try:
        n_docs = count_documents() if get_client().collection_exists(COLLECTION) else 0
    except Exception as e:
        sys.exit(f'Không kết nối được Qdrant ({type(e).__name__}). Chạy: docker compose up -d qdrant')
    if not n_docs:
        sys.exit('Collection Qdrant chưa có dữ liệu. Chạy: uv run python -m bank_faqs.vector_db')

    retrieval = eval_retrieval(cases)
    rows = []
    if not args.retrieval_only:
        df = pd.read_csv(MERGED_FILE, encoding='utf-8-sig')
        references = dict(zip(df['hoi_thoai_id'], df['answer']))
        target = build_target(args.target)
        judge = google_llm.with_structured_output(Verdict)
        try:
            for i, case in enumerate(cases, 1):
                print(f"[{i}/{len(cases)}] {case['id']}: {case['question']}", flush=True)
                rows.append(run_case(target, judge, case, references))
                if i < len(cases):
                    time.sleep(args.delay)
        except KeyboardInterrupt:
            print(f'\nDừng giữa chừng, giữ kết quả {len(rows)}/{len(cases)} câu đã chạy.')

    print_report(retrieval, rows)

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{args.target}.json"
    out.write_text(json.dumps({'target': args.target, 'retrieval': retrieval, 'cases': rows},
                              ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\nĐã lưu kết quả chi tiết: {out}')


if __name__ == '__main__':
    main()
