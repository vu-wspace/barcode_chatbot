import json
from pathlib import Path

import pandas as pd

from bank_faqs.config import MERGED_FILE
from bank_faqs.tools import TOOLS

# Không import evals/run_eval.py: script đó tắt LangSmith và đổi stdout khi import
DATASET = Path(__file__).resolve().parents[1] / 'evals' / 'dataset.jsonl'
FIELDS = {'id', 'category', 'question', 'tools', 'tools_optional', 'faq_id', 'must_contain', 'escalate', 'refuse', 'criteria'}


def load() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text(encoding='utf-8').splitlines() if line.strip()]


def test_cases_have_required_fields_and_unique_ids():
    cases = load()
    assert cases
    for c in cases:
        assert set(c) <= FIELDS, c['id']  # gõ sai tên trường thì script lặng lẽ bỏ qua
        assert c['question'].strip() and c['criteria'].strip() and c['category']
    ids = [c['id'] for c in cases]
    assert len(ids) == len(set(ids))


def test_expected_tools_exist():
    for c in load():
        assert set(c.get('tools', [])) <= set(TOOLS), c['id']


def test_faq_ids_exist_in_merged_data():
    known = set(pd.read_csv(MERGED_FILE, encoding='utf-8-sig')['hoi_thoai_id'])
    for c in load():
        if 'faq_id' in c:
            assert c['faq_id'] in known, c['id']


def test_questions_are_paraphrased_not_copied_from_data():
    # Câu eval trùng nguyên văn câu trong Qdrant thì truy xuất luôn đúng, chỉ số hit@k mất ý nghĩa
    stored = {q.strip().lower() for q in pd.read_csv(MERGED_FILE, encoding='utf-8-sig')['question']}
    for c in load():
        assert c['question'].strip().lower() not in stored, c['id']


def test_must_contain_is_list_of_keyword_groups():
    for c in load():
        for group in c.get('must_contain', []):
            assert isinstance(group, list) and group and all(isinstance(k, str) and k for k in group), c['id']
