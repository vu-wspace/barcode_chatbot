import bank_faqs  # noqa: F401 - phải import đầu tiên khi chạy file trực tiếp (xem __init__.py)
import uuid
import pandas as pd
from functools import lru_cache
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_qdrant import QdrantVectorStore
from pyvi import ViTokenizer
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams
from sentence_transformers import SentenceTransformer
from bank_faqs.config import COLLECTION, EMBEDDING_MODEL, MERGED_FILE, QDRANT_URL, VECTOR_SIZE

class BkaiEmbeddings(Embeddings):
    def __init__(self, model_name: str = EMBEDDING_MODEL):
        self.model = SentenceTransformer(model_name)

    def _enc(self, texts):
        texts = [ViTokenizer.tokenize(t) for t in texts]
        return self.model.encode(texts, normalize_embeddings=True).tolist()

    def embed_documents(self, texts):
        return self._enc(texts)

    def embed_query(self, text):
        return self._enc([text])[0]

@lru_cache
def get_embeddings() -> BkaiEmbeddings:
    # Model ~540MB: chỉ nạp 1 lần dù có nhiều vector store
    return BkaiEmbeddings()

@lru_cache
def get_client() -> QdrantClient:
    # Dùng chung 1 client: tạo mới mỗi lần (vd mỗi lần healthcheck gọi /health) sẽ tích tụ kết nối không đóng.
    # Tạo client không kết nối ngay, nên Qdrant khởi động lại thì client vẫn dùng được.
    return QdrantClient(url=QDRANT_URL)

def get_vector_store() -> QdrantVectorStore:
    client = get_client()
    if not client.collection_exists(COLLECTION):
        client.create_collection(
            COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
    return QdrantVectorStore(client=client, collection_name=COLLECTION, embedding=get_embeddings())

def count_documents() -> int:
    return get_client().count(COLLECTION).count

def ingest(db: QdrantVectorStore) -> int:
    df = pd.read_csv(MERGED_FILE, encoding='utf-8-sig')
    docs = [
        Document(page_content=q, metadata={'answer': a, 'chu_de': c, 'hoi_thoai_id': i})
        for i, q, a, c in zip(df['hoi_thoai_id'], df['question'], df['answer'], df['chu_de'])
    ]
    ids = [str(uuid.uuid5(uuid.NAMESPACE_DNS, i)) for i in df['hoi_thoai_id']]
    db.add_documents(docs, ids=ids)
    return len(docs)

def get_retriever(k: int = 3):
    return get_vector_store().as_retriever(search_kwargs={'k': k})

def main():
    print('1. Đang kết nối Qdrant + tải model...', flush=True)
    db = get_vector_store()

    print('2. Đang nạp dữ liệu...', flush=True)
    n = ingest(db)
    print(f"3. Đã nạp {n} hội thoại vào '{COLLECTION}'", flush=True)

    for h in get_retriever().invoke('quên mật khẩu app ngân hàng'):
        print(h.metadata['hoi_thoai_id'], '|', h.page_content[:60])

if __name__ == "__main__":
    main()
