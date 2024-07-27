import os
import random
import asyncio

import nest_asyncio
from llama_index.core.retrievers import AutoMergingRetriever
from llama_index.core.storage.docstore import SimpleDocumentStore
from llama_index.legacy.llms import OpenAILike as OpenAI
from qdrant_client import models

from .rag import generation
from ..custom.embeddings import GTEEmbedding, HuggingFaceEmbedding
from llama_index.core import Settings, StorageContext, QueryBundle, PromptTemplate
from .ingestion import read_data, build_pipeline, build_preprocess_pipeline, build_vector_store, get_node_content, build_qdrant_filters
from ..custom.rerankers import SentenceTransformerRerank, LLMRerank
from ..custom.retrievers import QdrantRetriever, BM25Retriever, HybridRetriever
from ..custom.hierarchical import get_leaf_nodes
from ..custom.template import QA_TEMPLATE


def load_stopwords(path):
    with open(path, 'r', encoding='utf-8') as file:
        stopwords = set([line.strip() for line in file])
    return stopwords


nest_asyncio.apply()


class EasyRAGPipeline:
    def __init__(
            self,
            config,
    ):
        self.config = config
        asyncio.get_event_loop().run_until_complete(self.async_init())

    async def async_init(self):
        config = self.config
        print("EasyRAGPipeline 初始化开始".center(60, "="))

        self.re_only = config["re_only"]
        self.llm_embed_type = config['llm_embed_type']
        self.r_topk_1 = config['r_topk_1']
        self.rerank_fusion_type = config['rerank_fusion_type']
        # 初始化 LLM
        llm_key = random.choice(config["llm_keys"])
        llm_name = config['llm_name']
        self.llm = OpenAI(
            api_key=llm_key,
            model=llm_name,
            api_base="https://open.bigmodel.cn/api/paas/v4/",
            is_chat_model=True,
        )
        self.qa_template = QA_TEMPLATE

        # 初始化Embedding模型
        retrieval_type = config['retrieval_type']
        embedding_name = config['embedding_name']
        f_embed_type_1 = config['f_embed_type_1']
        hfmodel_cache_folder = config['hfmodel_cache_folder']
        if retrieval_type != 2:
            if "gte" in embedding_name \
                    or "Zhihui" in embedding_name:
                embedding = GTEEmbedding(
                    model_name=embedding_name,
                    embed_batch_size=128,
                    embed_type=f_embed_type_1,
                )
            else:
                embedding = HuggingFaceEmbedding(
                    model_name=embedding_name,
                    cache_folder=hfmodel_cache_folder,
                    embed_batch_size=128,
                    embed_type=f_embed_type_1,
                    # query_instruction="为这个句子生成表示以用于检索相关文章：", # 默认已经加上了，所以加不加无所谓
                )
        else:
            embedding = None
        Settings.embed_model = embedding

        # 文档预处理成节点
        data_path = os.path.abspath(config['data_path'])
        chunk_size = config['chunk_size']
        chunk_overlap = config['chunk_overlap']
        data = read_data(data_path)
        print(f"文档读入完成，一共有{len(data)}个文档")
        vector_store = None
        if retrieval_type != 2:
            collection_name = config['collection_name']
            # 初始化 数据ingestion pipeline 和 vector store
            client, vector_store = await build_vector_store(
                qdrant_url=config['qdrant_url'],
                cache_path=config['cache_path'],
                reindex=config['reindex'],
                collection_name=collection_name,
                vector_size=config['vector_size'],
            )

            collection_info = await client.get_collection(
                collection_name=collection_name,
            )
            if collection_info.points_count == 0:
                pipeline = build_pipeline(
                    self.llm, embedding, vector_store=vector_store, data_path=data_path,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                )
                # 暂时停止实时索引
                await client.update_collection(
                    collection_name=collection_name,
                    optimizer_config=models.OptimizersConfigDiff(indexing_threshold=0),
                )
                nodes = await pipeline.arun(documents=data, show_progress=True, num_workers=1)
                # 恢复实时索引
                await client.update_collection(
                    collection_name=collection_name,
                    optimizer_config=models.OptimizersConfigDiff(indexing_threshold=20000),
                )
                print(f"索引建立完成，一共有{len(nodes)}个节点")
        split_type = config['split_type']
        preprocess_pipeline = build_preprocess_pipeline(
            data_path,
            chunk_size,
            chunk_overlap,
            split_type,
        )
        nodes_ = await preprocess_pipeline.arun(documents=data, show_progress=True, num_workers=1)
        print(f"索引已建立，一共有{len(nodes_)}个节点")

        # 加载密集检索
        f_topk_1 = config['f_topk_1']
        self.dense_retriever = QdrantRetriever(vector_store, embedding, similarity_top_k=f_topk_1)
        print(f"创建{embedding_name}密集检索器成功")

        # 加载稀疏检索
        stp_words = load_stopwords("./data/hit_stopwords.txt")
        import jieba
        tk = jieba.Tokenizer()
        if split_type == 1:
            self.nodes = get_leaf_nodes(nodes_)
            print("叶子节点数量:", len(self.nodes))
            docstore = SimpleDocumentStore()
            docstore.add_documents(self.nodes)
            storage_context = StorageContext.from_defaults(docstore=docstore)
        else:
            self.nodes = nodes_
        f_topk_2 = config['f_topk_2']
        f_embed_type_2 = config['f_embed_type_2']
        self.sparse_retriever = BM25Retriever.from_defaults(
            nodes=self.nodes,
            tokenizer=tk,
            similarity_top_k=f_topk_2,
            stopwords=stp_words,
            embed_type=f_embed_type_2,
        )
        # self.path_retriever = BM25Retriever.from_defaults(
        #     nodes=self.nodes,
        #     tokenizer=tk,
        #     similarity_top_k=192,
        #     stopwords=stp_words,
        #     embed_type=4,  # 4-->file_path 5-->know_path
        # )

        if split_type == 1:
            self.sparse_retriever = AutoMergingRetriever(
                self.sparse_retriever,
                storage_context,
                simple_ratio_thresh=0.4,
            )
        print("创建BM25稀疏检索器成功")

        # 创建node快速索引
        self.nodeid2idx = dict()
        for i, node in enumerate(self.nodes):
            self.nodeid2idx[node.node_id] = i

        # 创建检索器
        if retrieval_type == 1:
            self.retriever = self.dense_retriever
        elif retrieval_type == 2:
            self.retriever = self.sparse_retriever
        elif retrieval_type == 3:
            f_topk = config['f_topk']
            self.retriever = HybridRetriever(
                dense_retriever=self.dense_retriever,
                sparse_retriever=self.sparse_retriever,
                retrieval_type=retrieval_type,  # 1-dense 2-sparse 3-hybrid
                topk=f_topk,
            )
        print("创建混合检索器成功")

        # 创建重排器
        self.reranker = None
        use_reranker = config['use_reranker']
        r_topk = config['r_topk']
        reranker_name = config['reranker_name']
        r_embed_type = config['r_embed_type']
        if use_reranker == 1:
            self.reranker = SentenceTransformerRerank(
                top_n=r_topk,
                model=reranker_name,
            )
            print(f"创建{reranker_name}重排器成功")
        elif use_reranker == 2:
            self.reranker = LLMRerank(
                top_n=r_topk,
                model=reranker_name,
                embed_bs=32,  # 控制重排器批大小，减小显存占用
                embed_type=r_embed_type,
            )
            print(f"创建{reranker_name}LLM重排器成功")

        print("EasyRAGPipeline 初始化完成".center(60, "="))

    async def run(self, query: dict) -> dict:
        '''
        "query":"问题" #必填
        "document": "所属路径" #用于过滤文档，可选
        '''
        filters = None
        filter_dict = dict()
        if "document" in query:
            dir = query['document']
            filters = build_qdrant_filters(
                dir=dir
            )
            filter_dict = {
                "dir": dir
            }
        if self.rerank_fusion_type == 0:
            self.retriever.filters = filters
            self.retriever.filter_dict = filter_dict
            res = await self.generation_with_knowledge_retrieval(
                query_str=query["query"],
            )
        else:
            self.dense_retriever.filters = filters
            self.sparse_retriever.filter_dict = filter_dict
            res = await self.generation_with_rerank_fusion(
                query_str=query["query"],
            )
        return res

    async def generation_with_knowledge_retrieval(
            self,
            query_str: str,
    ):
        query_bundle = QueryBundle(query_str=query_str)
        # node_with_scores_path = await path_retriever.aretrieve(query_bundle)
        # analysis_path_res(query_str, node_with_scores_path)
        # return CompletionResponse(text=""), node_with_scores_path, [""]
        node_with_scores = await self.retriever.aretrieve(query_bundle)
        if self.reranker:
            node_with_scores = self.reranker.postprocess_nodes(node_with_scores, query_bundle)
        contents = [get_node_content(node, self.llm_embed_type, self.nodes, self.nodeid2idx) for node in node_with_scores]
        # contents = deduplicate(contents)
        context_str = "\n\n".join(
            [f"### 文档{i}: {content}" for i, content in enumerate(contents)]
        )
        if self.re_only:
            return {"answer": "", "nodes": node_with_scores, "contexts": contents}
        fmt_qa_prompt = PromptTemplate(self.qa_template).format(
            context_str=context_str, query_str=query_str
        )
        ret = await generation(self.llm, fmt_qa_prompt)
        return {"answer": ret.text, "nodes": node_with_scores, "contexts": contents}

    async def generation_with_rerank_fusion(
            self,
            query_str: str,
    ):
        query_bundle = QueryBundle(query_str=query_str)

        node_with_scores_dense = await self.dense_retriever.aretrieve(query_bundle)
        if self.reranker:
            node_with_scores_dense = self.reranker.postprocess_nodes(node_with_scores_dense, query_bundle)

        node_with_scores_sparse = await self.sparse_retriever.aretrieve(query_bundle)
        if self.reranker:
            node_with_scores_sparse = self.reranker.postprocess_nodes(node_with_scores_sparse, query_bundle)

        node_with_scores = HybridRetriever.reciprocal_rank_fusion([node_with_scores_sparse, node_with_scores_dense],
                                                                  topk=self.r_topk_1)
        # node_with_scores = HybridRetriever.fusion([node_with_scores_sparse, node_with_scores_dense], topk=reranker.top_n)

        if self.re_only:
            contents = [get_node_content(node, self.llm_embed_type, self.nodes, self.nodeid2idx) for node in node_with_scores]
            return {"answer": "", "nodes": node_with_scores, "contexts": contents}

        if self.rerank_fusion_type == 1:
            contents = [get_node_content(node, self.llm_embed_type, self.nodes, self.nodeid2idx) for node in node_with_scores]
            context_str = "\n\n".join(
                [f"### 文档{i}: {content}" for i, content in enumerate(contents)]
            )
            fmt_qa_prompt = PromptTemplate(self.qa_template).format(
                context_str=context_str, query_str=query_str
            )
            ret = await generation(self.llm, fmt_qa_prompt)
        else:
            contents = [get_node_content(node, self.llm_embed_type, self.nodes, self.nodeid2idx) for node in node_with_scores_sparse]
            context_str = "\n\n".join(
                [f"### 文档{i}: {content}" for i, content in enumerate(contents)]
            )
            fmt_qa_prompt = PromptTemplate(self.qa_template).format(
                context_str=context_str, query_str=query_str
            )
            ret_sparse = await generation(self.llm, fmt_qa_prompt)

            contents = [get_node_content(node, self.llm_embed_type, self.nodes, self.nodeid2idx) for node in node_with_scores_dense]
            context_str = "\n\n".join(
                [f"### 文档{i}: {content}" for i, content in enumerate(contents)]
            )
            fmt_qa_prompt = PromptTemplate(self.qa_template).format(
                context_str=context_str, query_str=query_str
            )
            ret_dense = await generation(self.llm, fmt_qa_prompt)

            if self.rerank_fusion_type == 2:
                if len(ret_dense.text) >= len(ret_sparse.text):
                    ret = ret_dense
                else:
                    ret = ret_sparse
            else:
                ret = ret_sparse + ret_dense

        return {"answer": ret.text, "nodes": node_with_scores, "contexts": contents}
