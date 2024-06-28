import asyncio
import json
import os

from pipeline.embeddings import GTEEmbedding
from submit import submit
import fire
from dotenv import dotenv_values
from llama_index.core import Settings
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.legacy.llms import OpenAILike as OpenAI
from qdrant_client import models
from tqdm.asyncio import tqdm
from pipeline.ingestion import build_pipeline, build_vector_store, read_data, build_qdrant_filters, build_preprocess_pipeline
from pipeline.qa import read_jsonl, save_answers
from pipeline.rag import generation_with_knowledge_retrieval
from pipeline.retrievers import QdrantRetriever, HybridRetriever, BM25Retriever
from config import GLM_KEY
from pipeline.rerankers import SentenceTransformerRerank, LLMRerank


def load_stopwords(path):
    with open(path, 'r', encoding='utf-8') as file:
        stopwords = set([line.strip() for line in file])
    return stopwords


def get_test_data(split="val"):
    if split == 'test':
        queries = read_jsonl("question.jsonl")
    else:
        with open("dataset/val.json") as f:
            queries = json.loads(f.read())
    return queries


async def main(
        split='test',  # 使用哪个集合
        push=False,  # 是否直接提交这次test结果
        save_inter=True,  # 是否保存检索结果等中间结果
        note="",  # 中间结果保存路径的备注名字
        reindex=False,  # 是否从头开始构建索引
        re_only=False,  # 只检索，用于调试检索
        retrieval_type=1,  # 粗排类型
        use_reranker=2,  # 是否使用重排
        f_topk=256,  # 粗排topk
        r_topk=6,  # 精排topk
        f_topk_1=288,  # dense 粗排topk
        f_topk_2=192,  # sparse 粗排topk
):
    config = dotenv_values(".env")
    # 初始化 LLM 嵌入模型 和 Reranker
    llm = OpenAI(
        api_key=GLM_KEY,
        model="glm-4",
        api_base="https://open.bigmodel.cn/api/paas/v4/",
        is_chat_model=True,
    )
    embedding_name = config.get("EMBEDDING_NAME")
    if "gte" in embedding_name:
        embedding = GTEEmbedding(
            model_name=embedding_name,
            embed_batch_size=128,
        )
    else:
        embedding = HuggingFaceEmbedding(
            model_name=embedding_name,
            cache_folder=config.get("HFMODEL_CACHE_FOLDER"),
            embed_batch_size=128,
            # query_instruction="为这个句子生成表示以用于检索相关文章：", # 默认已经加上了，所以加不加无所谓
        )
    Settings.embed_model = embedding

    # 初始化 数据ingestion pipeline 和 vector store
    client, vector_store = await build_vector_store(config, reindex=reindex)

    collection_info = await client.get_collection(
        config["COLLECTION_NAME"]
    )
    data_path = config.get("DATA_PATH")
    chunk_size = int(config.get("CHUNK_SIZE", 1024))
    chunk_overlap = int(config.get("CHUNK_OVERLAP", 50))
    data = read_data(data_path)
    print(f"文档读入完成，一共有{len(data)}个文档")
    if collection_info.points_count == 0:
        pipeline = build_pipeline(
            llm, embedding, vector_store=vector_store, data_path=data_path,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        # 暂时停止实时索引
        await client.update_collection(
            collection_name=config["COLLECTION_NAME"],
            optimizer_config=models.OptimizersConfigDiff(indexing_threshold=0),
        )
        nodes = await pipeline.arun(documents=data, show_progress=True, num_workers=1)
        # 恢复实时索引
        await client.update_collection(
            collection_name=config["COLLECTION_NAME"],
            optimizer_config=models.OptimizersConfigDiff(indexing_threshold=20000),
        )
        print(f"索引建立完成，一共有{len(nodes)}个节点")
    elif retrieval_type != 1:
        preprocess_pipeline = build_preprocess_pipeline(
            data_path,
            chunk_size,
            chunk_overlap,
        )
        nodes = await preprocess_pipeline.arun(documents=data, show_progress=True, num_workers=1)
        print(f"索引已建立，一共有{len(nodes)}个节点")

    # 加载检索器
    dense_retriever = QdrantRetriever(vector_store, embedding, similarity_top_k=f_topk_1)
    print(f"创建{config['EMBEDDING_NAME']}密集检索器成功")

    sparse_retriever = None
    if retrieval_type != 1:
        stp_words = load_stopwords("./data/hit_stopwords.txt")
        import jieba
        tk = jieba.Tokenizer()
        sparse_retriever = BM25Retriever.from_defaults(nodes=nodes, tokenizer=tk,
                                                       similarity_top_k=f_topk_2, stopwords=stp_words)
        print("创建稀疏检索器成功")

    if retrieval_type != 1:
        retriever = HybridRetriever(
            dense_retriever=dense_retriever,
            sparse_retriever=sparse_retriever,
            retrieval_type=retrieval_type,  # 1-dense 2-sparse 3-hybrid
            topk=f_topk,
        )
        print("创建混合检索器成功")
    else:
        retriever = dense_retriever

    reranker = None
    if use_reranker == 1:
        reranker = SentenceTransformerRerank(
            top_n=r_topk,
            model=config["RERANKER_NAME"],
        )
        print(f"创建{config['RERANKER_NAME']}重排器成功")
    elif use_reranker == 2:
        reranker = LLMRerank(
            top_n=r_topk,
            model=config["RERANKER_NAME"],
            embed_bs=64,  # 控制重排器批大小，减小显存占用
        )
        print(f"创建{config['RERANKER_NAME']}LLM重排器成功")

    # 读入测试集
    queries = get_test_data(split)

    # 生成答案
    print("开始生成答案...")

    results = []
    docs = []
    for query in tqdm(queries, total=len(queries)):
        if "document" in query:
            dir = query['document']
            filters = build_qdrant_filters(
                dir=dir
            )
        else:
            filters = None
        retriever.filters = filters
        retriever.filter_dict = {
            "dir": dir
        }
        result, contexts = await generation_with_knowledge_retrieval(
            query_str=query["query"],
            retriever=retriever,
            llm=llm,
            re_only=re_only,
            reranker=reranker,
        )
        docs.append(contexts)
        results.append(result)

    # 处理结果
    print("处理生成内容...")
    os.makedirs("outputs", exist_ok=True)
    answer_file = f"outputs/submit_result_{split}_{note}.jsonl"
    answers = save_answers(queries, results, answer_file)
    print(f"保存结果至 {answer_file}")

    # 做评测
    os.makedirs("inter", exist_ok=True)
    N = len(queries)
    if split == 'test':
        if push:
            judge_res = submit(answers)
            print(judge_res)
    elif split == 'val':
        all_keyword_acc = 0
        all_sim_acc = 0
        for answer_obj, gt_obj in tqdm(zip(answers, queries)):
            answer = answer_obj['answer']
            keywords = gt_obj['keywords']
            gt = gt_obj['answer']
            M = len(keywords)
            keyword_acc = 0
            for keyword in keywords:
                if keyword in answer:
                    keyword_acc += 1
            keyword_acc /= M
            all_keyword_acc += keyword_acc
        all_keyword_acc /= N
        all_sim_acc /= N
        acc = all_keyword_acc
        print("average acc:", acc * 100)

    # 保存中间结果
    if save_inter:
        print("保存中间结果...")
        inter_res_list = []
        for query, answer, documents in tqdm(zip(queries, answers, docs)):
            contexts = [f"{doc.metadata['document_title']}: {doc.text}" for doc in documents]
            paths = [doc.metadata['file_path'] for doc in documents]
            inter_res = {
                "id": query['id'],
                "query": query['query'],
                "answer": answer['answer'],
                "candidates": contexts,
                "paths": paths,
                "quality": [0 for _ in range(len(contexts))],
                "score": 0,
                "duplicate": 0,
            }
            if 'keywords' in query:
                inter_res['keywords'] = query['keywords']
                inter_res['gt'] = query['answer']
            inter_res_list.append(inter_res)
        inter_file = f"inter/{split}_{note}.json"
        with open(f"{inter_file}", 'w') as f:
            f.write(json.dumps(inter_res_list, ensure_ascii=False, indent=4))
        print(f"保存中间结果至 {inter_file}")


if __name__ == "__main__":
    fire.Fire(main)
