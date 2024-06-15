import os
from typing import List, Dict, Any

from llama_index.core import SimpleDirectoryReader
from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.extractors import SummaryExtractor, BaseExtractor
from llama_index.core.ingestion import IngestionPipeline
from llama_index.core.llms.llm import LLM
from llama_index.core.vector_stores.types import BasePydanticVectorStore, MetadataFilters, MetadataFilter, FilterOperator, ExactMatchFilter
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import Document, MetadataMode
from llama_index.vector_stores.qdrant import QdrantVectorStore
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.http.models import Filter, FieldCondition, MatchValue

from custom.template import SUMMARY_EXTRACT_TEMPLATE
from custom.transformation import CustomFilePathExtractor, CustomTitleExtractor


def read_data(path: str = "data") -> list[Document]:
    reader = SimpleDirectoryReader(
        input_dir=path,
        recursive=True,
        required_exts=[
            ".txt",
        ],
    )
    return reader.load_data()


class MyExtractor(BaseExtractor):
    data_path: str

    def __init__(self, data_path=None, **data: Any):
        super().__init__(data_path=data_path, **data)

    async def aextract(self, nodes) -> List[Dict]:
        metadata_list = [
            {
                "dir": node.metadata["file_path"].replace(self.data_path + "/", "").split("/")[0]
            }
            for node in nodes
        ]
        return metadata_list


def build_pipeline(
        llm: LLM,
        embed_model: BaseEmbedding,
        template: str = None,
        vector_store: BasePydanticVectorStore = None,
        data_path=None
) -> IngestionPipeline:
    transformation = [
        SentenceSplitter(chunk_size=1024, chunk_overlap=50),
        CustomTitleExtractor(metadata_mode=MetadataMode.EMBED),
        CustomFilePathExtractor(last_path_length=100000, metadata_mode=MetadataMode.EMBED),
        MyExtractor(data_path=data_path),
        # SummaryExtractor(
        #     llm=llm,
        #     metadata_mode=MetadataMode.EMBED,
        #     prompt_template=template or SUMMARY_EXTRACT_TEMPLATE,
        # ),
        embed_model,
    ]

    return IngestionPipeline(transformations=transformation, vector_store=vector_store)


async def build_vector_store(
        config: dict, reindex: bool = False
) -> tuple[AsyncQdrantClient, QdrantVectorStore]:
    client = AsyncQdrantClient(
        # url=config["QDRANT_URL"],
        # location=":memory:",
        path="cache/",
    )
    if reindex:
        try:
            await client.delete_collection(config["COLLECTION_NAME"] or "aiops24")
        except UnexpectedResponse as e:
            print(f"Collection not found: {e}")

    try:
        await client.create_collection(
            collection_name=config["COLLECTION_NAME"] or "aiops24",
            vectors_config=models.VectorParams(
                size=config["VECTOR_SIZE"] or 1024, distance=models.Distance.DOT
            ),
        )
    except Exception as e:
        print("Collection already exists")
    return client, QdrantVectorStore(
        aclient=client,
        collection_name=config["COLLECTION_NAME"] or "aiops24",
        parallel=4,
        batch_size=32,
    )


def build_filters(dir):
    filters = MetadataFilters(
        filters=[
            MetadataFilter(
                key="dir",
                # operator=FilterOperator.EQ,
                value=dir,
            ),
        ]
    )
    return filters


def build_qdrant_filters(dir):
    filters = Filter(
        must=[
            FieldCondition(
                key="dir",
                match=MatchValue(value=dir),
            )
        ]
    )
    return filters
