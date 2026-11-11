# Gộp các lượt nói của từng hội thoại CSKH thành 1 dòng (question/answer) để nạp vào vector DB
from pathlib import Path
import pandas as pd
from bank_faqs.config import CONVERSATIONS_FILE, MERGED_FILE

def load_conversation_data(path: Path = CONVERSATIONS_FILE) -> pd.DataFrame:
    df = pd.read_csv(path, encoding='utf-8-sig')
    return df.sort_values(['hoi_thoai_id', 'luot']).reset_index(drop=True)

def merge_conversation_by_id(df: pd.DataFrame, sep: str = ' ') -> pd.DataFrame:
    return (
        df.assign(
            question=df['noi_dung'].where(df['vai_tro'] == 'khach_hang'),
            answer=df['noi_dung'].where(df['vai_tro'] == 'nhan_vien'),
        )
        .groupby('hoi_thoai_id', sort=False)
        .agg(
            question=('question', lambda s: sep.join(s.dropna())),
            answer=('answer', lambda s: sep.join(s.dropna())),
            chu_de=('chu_de', 'first'),
        )
        .reset_index()
    )

def get_conversation_by_id(df: pd.DataFrame, conv_id: str) -> pd.DataFrame:
    conv = df[df['hoi_thoai_id'] == conv_id].sort_values('luot')
    if conv.empty:
        raise ValueError(f'Không tìm thấy id hội thoại {conv_id}')
    return conv

def save_csv(df: pd.DataFrame, path: Path = MERGED_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        df.to_csv(f, index=False)

def main() -> None:
    df = load_conversation_data()
    output = merge_conversation_by_id(df)
    save_csv(output)

    print(f'Đã gộp {len(df)} lượt -> {len(output)} hội thoại\n')
    print(f'Lưu tại: {MERGED_FILE}')
    print(output.head())

    print('\nVí dụ hội thoại HT001:')
    for r in get_conversation_by_id(df, 'HT001').itertuples():
        print(f'  [{r.luot}] {r.vai_tro}: {r.noi_dung}')

if __name__ == '__main__':
    main()
