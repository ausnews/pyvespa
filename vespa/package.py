import sys
import http.client
import json
import os
import re
import zipfile
from base64 import standard_b64encode
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from time import sleep, strftime, gmtime
from typing import List, Mapping, Optional, IO, Union, Dict
from shutil import copyfile

import docker
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from jinja2 import Environment, PackageLoader, select_autoescape

from vespa.json_serialization import ToJson, FromJson
from vespa.application import Vespa
from vespa.ml import ModelConfig, BertModelConfig


class HNSW(ToJson, FromJson["HNSW"]):
    def __init__(
        self,
        distance_metric="euclidean",
        max_links_per_node=16,
        neighbors_to_explore_at_insert=200,
    ):
        """
        Configure Vespa HNSW indexes

        :param distance_metric: Distance metric to use when computing distance between vectors. Default is 'euclidean'.
        :param max_links_per_node: Specifies how many links per HNSW node to select when building the graph.
            Default is 16.
        :param neighbors_to_explore_at_insert: Specifies how many neighbors to explore when inserting a document in
            the HNSW graph. Default is 200.
        """
        self.distance_metric = distance_metric
        self.max_links_per_node = max_links_per_node
        self.neighbors_to_explore_at_insert = neighbors_to_explore_at_insert

    @staticmethod
    def from_dict(mapping: Mapping) -> "HNSW":
        return HNSW(
            distance_metric=mapping["distance_metric"],
            max_links_per_node=mapping["max_links_per_node"],
            neighbors_to_explore_at_insert=mapping["neighbors_to_explore_at_insert"],
        )

    @property
    def to_dict(self) -> Mapping:
        map = {
            "distance_metric": self.distance_metric,
            "max_links_per_node": self.max_links_per_node,
            "neighbors_to_explore_at_insert": self.neighbors_to_explore_at_insert,
        }
        return map

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return (
            self.distance_metric == other.distance_metric
            and self.max_links_per_node == other.max_links_per_node
            and self.neighbors_to_explore_at_insert
            == other.neighbors_to_explore_at_insert
        )

    def __repr__(self):
        return "{0}({1}, {2}, {3})".format(
            self.__class__.__name__,
            repr(self.distance_metric),
            repr(self.max_links_per_node),
            repr(self.neighbors_to_explore_at_insert),
        )


class Field(ToJson, FromJson["Field"]):
    def __init__(
        self,
        name: str,
        type: str,
        indexing: Optional[List[str]] = None,
        index: Optional[str] = None,
        attribute: Optional[List[str]] = None,
        ann: Optional[HNSW] = None,
    ) -> None:
        """
        Create a Vespa field.

        Check the `Vespa documentation <https://docs.vespa.ai/documentation/reference/schema-reference.html#field>`_
        for more detailed information about fields.

        :param name: Field name.
        :param type: Field data type.
        :param indexing: Configures how to process data of a field during indexing.
        :param index: Sets index parameters. Content in fields with index are normalized and tokenized by default.
        :param attribute:  Specifies a property of an index structure attribute.
        :param ann: Add configuration for approximate nearest neighbor.

        >>> Field(name = "title", type = "string", indexing = ["index", "summary"], index = "enable-bm25")
        Field('title', 'string', ['index', 'summary'], 'enable-bm25', None, None)

        >>> Field(
        ...     name = "abstract",
        ...     type = "string",
        ...     indexing = ["attribute"],
        ...     attribute=["fast-search", "fast-access"]
        ... )
        Field('abstract', 'string', ['attribute'], None, ['fast-search', 'fast-access'], None)

        >>> Field(name="tensor_field",
        ...     type="tensor<float>(x[128])",
        ...     indexing=["attribute"],
        ...     ann=HNSW(
        ...         distance_metric="enclidean",
        ...         max_links_per_node=16,
        ...         neighbors_to_explore_at_insert=200,
        ...     ),
        ... )
        Field('tensor_field', 'tensor<float>(x[128])', ['attribute'], None, None, HNSW('enclidean', 16, 200))

        """
        self.name = name
        self.type = type
        self.indexing = indexing
        self.attribute = attribute
        self.index = index
        self.ann = ann

    @property
    def indexing_to_text(self) -> Optional[str]:
        if self.indexing is not None:
            return " | ".join(self.indexing)

    @staticmethod
    def from_dict(mapping: Mapping) -> "Field":
        ann = mapping.get("ann", None)
        return Field(
            name=mapping["name"],
            type=mapping["type"],
            indexing=mapping.get("indexing", None),
            index=mapping.get("index", None),
            attribute=mapping.get("attribute", None),
            ann=FromJson.map(ann) if ann is not None else None,
        )

    @property
    def to_dict(self) -> Mapping:
        map = {"name": self.name, "type": self.type}
        if self.indexing is not None:
            map.update(indexing=self.indexing)
        if self.index is not None:
            map.update(index=self.index)
        if self.attribute is not None:
            map.update(attribute=self.attribute)
        if self.ann is not None:
            map.update(ann=self.ann.to_envelope)
        return map

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return (
            self.name == other.name
            and self.type == other.type
            and self.indexing == other.indexing
            and self.index == other.index
            and self.attribute == other.attribute
            and self.ann == other.ann
        )

    def __repr__(self):
        return "{0}({1}, {2}, {3}, {4}, {5}, {6})".format(
            self.__class__.__name__,
            repr(self.name),
            repr(self.type),
            repr(self.indexing),
            repr(self.index),
            repr(self.attribute),
            repr(self.ann),
        )


class Document(ToJson, FromJson["Document"]):
    def __init__(self, fields: Optional[List[Field]] = None) -> None:
        """
        Create a Vespa Document.

        Check the `Vespa documentation <https://docs.vespa.ai/documentation/documents.html>`_
        for more detailed information about documents.

        :param fields: A list of :class:`Field` to include in the document's schema.

        To create a Document:

        >>> Document()
        Document(None)

        >>> Document(fields=[Field(name="title", type="string")])
        Document([Field('title', 'string', None, None, None, None)])

        """
        self.fields = [] if not fields else fields

    def add_fields(self, *fields: Field) -> None:
        """
        Add :class:`Field`'s to the document.

        :param fields: fields to be added
        :return:
        """
        self.fields.extend(fields)

    @staticmethod
    def from_dict(mapping: Mapping) -> "Document":
        return Document(fields=[FromJson.map(field) for field in mapping.get("fields")])

    @property
    def to_dict(self) -> Mapping:
        map = {"fields": [field.to_envelope for field in self.fields]}
        return map

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return self.fields == other.fields

    def __repr__(self):
        return "{0}({1})".format(
            self.__class__.__name__, repr(self.fields) if self.fields else None
        )


class FieldSet(ToJson, FromJson["FieldSet"]):
    def __init__(self, name: str, fields: List[str]) -> None:
        """
        Create a Vespa field set.

        A fieldset groups fields together for searching. Check the
        `Vespa documentation <https://docs.vespa.ai/documentation/reference/schema-reference.html#fieldset>`_
        for more detailed information about field sets.

        :param name: Name of the fieldset
        :param fields: Field names to be included in the fieldset.

        >>> FieldSet(name="default", fields=["title", "body"])
        FieldSet('default', ['title', 'body'])
        """
        self.name = name
        self.fields = fields

    @property
    def fields_to_text(self):
        if self.fields is not None:
            return ", ".join(self.fields)

    @staticmethod
    def from_dict(mapping: Mapping) -> "FieldSet":
        return FieldSet(name=mapping["name"], fields=mapping["fields"])

    @property
    def to_dict(self) -> Mapping:
        map = {"name": self.name, "fields": self.fields}
        return map

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return self.name == other.name and self.fields == other.fields

    def __repr__(self):
        return "{0}({1}, {2})".format(
            self.__class__.__name__, repr(self.name), repr(self.fields)
        )


class Function(ToJson, FromJson["Function"]):
    def __init__(
        self, name: str, expression: str, args: Optional[List[str]] = None
    ) -> None:
        r"""
        Create a Vespa rank function.

        Define a named function that can be referenced as a part of the ranking expression, or (if having no arguments)
        as a feature. Check the
        `Vespa documentation <https://docs.vespa.ai/documentation/reference/schema-reference.html#function-rank>`_
        for more detailed information about rank functions.

        :param name: Name of the function.
        :param expression: String representing a Vespa expression.
        :param args: Optional. List of arguments to be used in the function expression.

        >>> Function(
        ...     name="myfeature",
        ...     expression="fieldMatch(bar) + freshness(foo)",
        ...     args=["foo", "bar"]
        ... )
        Function('myfeature', 'fieldMatch(bar) + freshness(foo)', ['foo', 'bar'])

        It is possible to define functions with multi-line expressions:

        >>> Function(
        ...     name="token_type_ids",
        ...     expression="tensor<float>(d0[1],d1[128])(\n"
        ...                "    if (d1 < question_length,\n"
        ...                "        0,\n"
        ...                "    if (d1 < question_length + doc_length,\n"
        ...                "        1,\n"
        ...                "        TOKEN_NONE\n"
        ...                "    )))",
        ... )
        Function('token_type_ids', 'tensor<float>(d0[1],d1[128])(\n    if (d1 < question_length,\n        0,\n    if (d1 < question_length + doc_length,\n        1,\n        TOKEN_NONE\n    )))', None)
        """
        self.name = name
        self.args = args
        self.expression = expression

    @property
    def args_to_text(self) -> str:
        if self.args is not None:
            return ", ".join(self.args)
        else:
            return ""

    @staticmethod
    def from_dict(mapping: Mapping) -> "Function":
        return Function(
            name=mapping["name"], expression=mapping["expression"], args=mapping["args"]
        )

    @property
    def to_dict(self) -> Mapping:
        map = {"name": self.name, "expression": self.expression, "args": self.args}
        return map

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return (
            self.name == other.name
            and self.expression == other.expression
            and self.args == other.args
        )

    def __repr__(self):
        return "{0}({1}, {2}, {3})".format(
            self.__class__.__name__,
            repr(self.name),
            repr(self.expression),
            repr(self.args),
        )


class SecondPhaseRanking(ToJson, FromJson["SecondPhaseRanking"]):
    def __init__(self, expression: str, rerank_count: int = 100) -> None:
        r"""
        Create a Vespa second phase ranking configuration.

        This is the optional reranking performed on the best hits from the first phase. Check the
        `Vespa documentation <https://docs.vespa.ai/documentation/reference/schema-reference.html#secondphase-rank>`_
        for more detailed information about second phase ranking configuration.

        :param expression: Specify the ranking expression to be used for second phase of ranking. Check also the
            `Vespa documentation <https://docs.vespa.ai/documentation/reference/ranking-expressions.html>`_
            for ranking expression.
        :param rerank_count: Specifies the number of hits to be reranked in the second phase. Default value is 100.

        >>> SecondPhaseRanking(expression="1.25 * bm25(title) + 3.75 * bm25(body)", rerank_count=10)
        SecondPhaseRanking('1.25 * bm25(title) + 3.75 * bm25(body)', 10)
        """
        self.expression = expression
        self.rerank_count = rerank_count

    @staticmethod
    def from_dict(mapping: Mapping) -> "SecondPhaseRanking":
        return SecondPhaseRanking(
            expression=mapping["expression"], rerank_count=mapping["rerank_count"]
        )

    @property
    def to_dict(self) -> Mapping:
        map = {"expression": self.expression, "rerank_count": self.rerank_count}
        return map

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return (
            self.expression == other.expression
            and self.rerank_count == other.rerank_count
        )

    def __repr__(self):
        return "{0}({1}, {2})".format(
            self.__class__.__name__,
            repr(self.expression),
            repr(self.rerank_count),
        )


class RankProfile(ToJson, FromJson["RankProfile"]):
    def __init__(
        self,
        name: str,
        first_phase: str,
        inherits: Optional[str] = None,
        constants: Optional[Dict] = None,
        functions: Optional[List[Function]] = None,
        summary_features: Optional[List] = None,
        second_phase: Optional[SecondPhaseRanking] = None,
    ) -> None:
        """
        Create a Vespa rank profile.

        Rank profiles are used to specify an alternative ranking of the same data for different purposes, and to
        experiment with new rank settings. Check the
        `Vespa documentation <https://docs.vespa.ai/documentation/reference/schema-reference.html#rank-profile>`_
        for more detailed information about rank profiles.

        :param name: Rank profile name.
        :param first_phase: The config specifying the first phase of ranking.
            `More info <https://docs.vespa.ai/documentation/reference/schema-reference.html#firstphase-rank>`_
            about first phase ranking.
        :param inherits: The inherits attribute is optional. If defined, it contains the name of one other
            rank profile in the same schema. Values not defined in this rank profile will then be inherited.
        :param constants: Dict of constants available in ranking expressions, resolved and optimized at
            configuration time.
            `More info <https://docs.vespa.ai/documentation/reference/schema-reference.html#constants>`_
            about constants.
        :param functions: Optional list of :class:`Function` representing rank functions to be included in the rank
            profile.
        :param summary_features: List of rank features to be included with each hit.
            `More info <https://docs.vespa.ai/documentation/reference/schema-reference.html#summary-features>`_
            about summary features.
        :param second_phase: Optional config specifying the second phase of ranking.
            See :class:`SecondPhaseRanking`.

        >>> RankProfile(name = "default", first_phase = "nativeRank(title, body)")
        RankProfile('default', 'nativeRank(title, body)', None, None, None, None, None)

        >>> RankProfile(name = "new", first_phase = "BM25(title)", inherits = "default")
        RankProfile('new', 'BM25(title)', 'default', None, None, None, None)

        >>> RankProfile(
        ...     name = "new",
        ...     first_phase = "BM25(title)",
        ...     inherits = "default",
        ...     constants={"TOKEN_NONE": 0, "TOKEN_CLS": 101, "TOKEN_SEP": 102},
        ...     summary_features=["BM25(title)"]
        ... )
        RankProfile('new', 'BM25(title)', 'default', {'TOKEN_NONE': 0, 'TOKEN_CLS': 101, 'TOKEN_SEP': 102}, None, ['BM25(title)'], None)

        >>> RankProfile(
        ...     name="bert",
        ...     first_phase="bm25(title) + bm25(body)",
        ...     second_phase=SecondPhaseRanking(expression="1.25 * bm25(title) + 3.75 * bm25(body)", rerank_count=10),
        ...     inherits="default",
        ...     constants={"TOKEN_NONE": 0, "TOKEN_CLS": 101, "TOKEN_SEP": 102},
        ...     functions=[
        ...         Function(
        ...             name="question_length",
        ...             expression="sum(map(query(query_token_ids), f(a)(a > 0)))"
        ...         ),
        ...         Function(
        ...             name="doc_length",
        ...             expression="sum(map(attribute(doc_token_ids), f(a)(a > 0)))"
        ...         )
        ...     ],
        ...     summary_features=["question_length", "doc_length"]
        ... )
        RankProfile('bert', 'bm25(title) + bm25(body)', 'default', {'TOKEN_NONE': 0, 'TOKEN_CLS': 101, 'TOKEN_SEP': 102}, [Function('question_length', 'sum(map(query(query_token_ids), f(a)(a > 0)))', None), Function('doc_length', 'sum(map(attribute(doc_token_ids), f(a)(a > 0)))', None)], ['question_length', 'doc_length'], SecondPhaseRanking('1.25 * bm25(title) + 3.75 * bm25(body)', 10))
        """
        self.name = name
        self.first_phase = first_phase
        self.inherits = inherits
        self.constants = constants
        self.functions = functions
        self.summary_features = summary_features
        self.second_phase = second_phase

    @staticmethod
    def from_dict(mapping: Mapping) -> "RankProfile":
        functions = mapping.get("functions", None)
        if functions is not None:
            functions = [FromJson.map(f) for f in functions]
        second_phase = mapping.get("second_phase", None)
        if second_phase is not None:
            second_phase = FromJson.map(second_phase)

        return RankProfile(
            name=mapping["name"],
            first_phase=mapping["first_phase"],
            inherits=mapping.get("inherits", None),
            constants=mapping.get("constants", None),
            functions=functions,
            summary_features=mapping.get("summary_features", None),
            second_phase=second_phase,
        )

    @property
    def to_dict(self) -> Mapping:
        map = {
            "name": self.name,
            "first_phase": self.first_phase,
        }
        if self.inherits is not None:
            map.update({"inherits": self.inherits})
        if self.constants is not None:
            map.update({"constants": self.constants})
        if self.functions is not None:
            map.update({"functions": [f.to_envelope for f in self.functions]})
        if self.summary_features is not None:
            map.update({"summary_features": self.summary_features})
        if self.second_phase is not None:
            map.update({"second_phase": self.second_phase.to_envelope})

        return map

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return (
            self.name == other.name
            and self.first_phase == other.first_phase
            and self.inherits == other.inherits
            and self.constants == other.constants
            and self.functions == other.functions
            and self.summary_features == other.summary_features
            and self.second_phase == other.second_phase
        )

    def __repr__(self):
        return "{0}({1}, {2}, {3}, {4}, {5}, {6}, {7})".format(
            self.__class__.__name__,
            repr(self.name),
            repr(self.first_phase),
            repr(self.inherits),
            repr(self.constants),
            repr(self.functions),
            repr(self.summary_features),
            repr(self.second_phase),
        )


class OnnxModel(ToJson, FromJson["OnnxModel"]):
    def __init__(
        self,
        model_name: str,
        model_file_path: str,
        inputs: Dict[str, str],
        outputs: Dict[str, str],
    ) -> None:
        """
        Create a Vespa ONNX model config.

        Vespa has support for advanced ranking models through it’s tensor API. If you have your model in the ONNX
        format, Vespa can import the models and use them directly. Check the
        `Vespa documentation <https://docs.vespa.ai/documentation/onnx.html>`_
        for more detailed information about field sets.

        :param model_name: Unique model name to use as id when referencing the model.
        :param model_file_path: ONNX model file path.
        :param inputs: Dict mapping the ONNX input names as specified in the ONNX file to valid Vespa inputs,
            which can be a document field (`attribute(field_name)`), a query parameter (`query(query_param)`),
            a constant (`constant(name)`) and a user-defined function (`function_name`).
        :param outputs: Dict mapping the ONNX output names as specified in the ONNX file to the name used in Vespa to
            specify the output. If this is omitted, the first output in the ONNX file will be used.

        >>> OnnxModel(
        ...     model_name="bert",
        ...     model_file_path="bert.onnx",
        ...     inputs={
        ...         "input_ids": "input_ids",
        ...         "token_type_ids": "token_type_ids",
        ...         "attention_mask": "attention_mask",
        ...     },
        ...     outputs={"logits": "logits"},
        ... )
        OnnxModel('bert', 'bert.onnx', {'input_ids': 'input_ids', 'token_type_ids': 'token_type_ids', 'attention_mask': 'attention_mask'}, {'logits': 'logits'})
        """
        self.model_name = model_name
        self.model_file_path = model_file_path
        self.inputs = inputs
        self.outputs = outputs

        self.model_file_name = self.model_name + ".onnx"
        self.file_path = os.path.join("files", self.model_file_name)

    @staticmethod
    def from_dict(mapping: Mapping) -> "OnnxModel":
        return OnnxModel(
            model_name=mapping["model_name"],
            model_file_path=mapping["model_file_path"],
            inputs=mapping["inputs"],
            outputs=mapping["outputs"],
        )

    @property
    def to_dict(self) -> Mapping:
        map = {
            "model_name": self.model_name,
            "model_file_path": self.model_file_path,
            "inputs": self.inputs,
            "outputs": self.outputs,
        }
        return map

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return (
            self.model_name == other.model_name
            and self.model_file_path == other.model_file_path
            and self.inputs == other.inputs
            and self.outputs == other.outputs
        )

    def __repr__(self):
        return "{0}({1}, {2}, {3}, {4})".format(
            self.__class__.__name__,
            repr(self.model_name),
            repr(self.model_file_path),
            repr(self.inputs),
            repr(self.outputs),
        )


class Schema(ToJson, FromJson["Schema"]):
    def __init__(
        self,
        name: str,
        document: Document,
        fieldsets: Optional[List[FieldSet]] = None,
        rank_profiles: Optional[List[RankProfile]] = None,
        models: Optional[List[OnnxModel]] = None,
    ) -> None:
        """
        Create a Vespa Schema.

        Check the `Vespa documentation <https://docs.vespa.ai/documentation/schemas.html>`_
        for more detailed information about schemas.

        :param name: Schema name.
        :param document: Vespa :class:`Document` associated with the Schema.
        :param fieldsets: A list of :class:`FieldSet` associated with the Schema.
        :param rank_profiles: A list of :class:`RankProfile` associated with the Schema.
        :param models: A list of :class:`OnnxModel` associated with the Schema.

        To create a Schema:

        >>> Schema(name="schema_name", document=Document())
        Schema('schema_name', Document(None), None, None, [])
        """
        self.name = name
        self.document = document

        self.fieldsets = {}
        if fieldsets is not None:
            self.fieldsets = {fieldset.name: fieldset for fieldset in fieldsets}

        self.rank_profiles = {}
        if rank_profiles is not None:
            self.rank_profiles = {
                rank_profile.name: rank_profile for rank_profile in rank_profiles
            }

        self.models = [] if models is None else list(models)

    def add_fields(self, *fields: Field) -> None:
        """
        Add :class:`Field` to the Schema's :class:`Document`.

        :param fields: fields to be added.
        """
        self.document.add_fields(*fields)

    def add_field_set(self, field_set: FieldSet) -> None:
        """
        Add a :class:`FieldSet` to the Schema.

        :param field_set: field sets to be added.
        """
        self.fieldsets[field_set.name] = field_set

    def add_rank_profile(self, rank_profile: RankProfile) -> None:
        """
        Add a :class:`RankProfile` to the Schema.

        :param rank_profile: rank profile to be added.
        :return: None.
        """
        self.rank_profiles[rank_profile.name] = rank_profile

    def add_model(self, model: OnnxModel) -> None:
        """
        Add a :class:`OnnxModel` to the Schema.
        :param model: model to be added.
        :return: None.
        """
        self.models.append(model)

    @staticmethod
    def from_dict(mapping: Mapping) -> "Schema":
        fieldsets = mapping.get("fieldsets", None)
        if fieldsets:
            fieldsets = [FromJson.map(fieldset) for fieldset in mapping["fieldsets"]]
        rank_profiles = mapping.get("rank_profiles", None)
        if rank_profiles:
            rank_profiles = [
                FromJson.map(rank_profile) for rank_profile in mapping["rank_profiles"]
            ]
        models = mapping.get("models", None)
        if models:
            models = [FromJson.map(model) for model in mapping["models"]]

        return Schema(
            name=mapping["name"],
            document=FromJson.map(mapping["document"]),
            fieldsets=fieldsets,
            rank_profiles=rank_profiles,
            models=models,
        )

    @property
    def to_dict(self) -> Mapping:
        map = {
            "name": self.name,
            "document": self.document.to_envelope,
        }
        if self.fieldsets:
            map["fieldsets"] = [
                self.fieldsets[name].to_envelope for name in self.fieldsets.keys()
            ]
        if self.rank_profiles:
            map["rank_profiles"] = [
                self.rank_profiles[name].to_envelope
                for name in self.rank_profiles.keys()
            ]
        if self.models:
            map["models"] = [model.to_envelope for model in self.models]

        return map

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return (
            self.name == other.name
            and self.document == other.document
            and self.fieldsets == other.fieldsets
            and self.rank_profiles == other.rank_profiles
            and self.models == other.models
        )

    def __repr__(self):
        return "{0}({1}, {2}, {3}, {4}, {5})".format(
            self.__class__.__name__,
            repr(self.name),
            repr(self.document),
            repr(
                [field for field in self.fieldsets.values()] if self.fieldsets else None
            ),
            repr(
                [rank_profile for rank_profile in self.rank_profiles.values()]
                if self.rank_profiles
                else None
            ),
            repr(self.models),
        )


class QueryTypeField(ToJson, FromJson["QueryTypeField"]):
    def __init__(
        self,
        name: str,
        type: str,
    ) -> None:
        """
        Create a field to be included in a :class:`QueryProfileType`.

        :param name: Field name.
        :param type: Field type.

        >>> QueryTypeField(
        ...     name="ranking.features.query(title_bert)",
        ...     type="tensor<float>(x[768])"
        ... )
        QueryTypeField('ranking.features.query(title_bert)', 'tensor<float>(x[768])')
        """
        self.name = name
        self.type = type

    @staticmethod
    def from_dict(mapping: Mapping) -> "QueryTypeField":
        return QueryTypeField(
            name=mapping["name"],
            type=mapping["type"],
        )

    @property
    def to_dict(self) -> Mapping:
        map = {"name": self.name, "type": self.type}
        return map

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return self.name == other.name and self.type == other.type

    def __repr__(self):
        return "{0}({1}, {2})".format(
            self.__class__.__name__,
            repr(self.name),
            repr(self.type),
        )


class QueryProfileType(ToJson, FromJson["QueryProfileType"]):
    def __init__(self, fields: Optional[List[QueryTypeField]] = None) -> None:
        """
        Create a Vespa Query Profile Type.

        Check the `Vespa documentation <https://docs.vespa.ai/documentation/query-profiles.html#query-profile-types>`_
        for more detailed information about query profile types.

        :param fields: A list of :class:`QueryTypeField`.

        >>> QueryProfileType(
        ...     fields = [
        ...         QueryTypeField(
        ...             name="ranking.features.query(tensor_bert)",
        ...             type="tensor<float>(x[768])"
        ...         )
        ...     ]
        ... )
        QueryProfileType([QueryTypeField('ranking.features.query(tensor_bert)', 'tensor<float>(x[768])')])
        """
        self.name = "root"
        self.fields = [] if not fields else fields

    def add_fields(self, *fields: QueryTypeField) -> None:
        """
        Add :class:`QueryTypeField`'s to the Query Profile Type.

        :param fields: fields to be added

        >>> query_profile_type = QueryProfileType()
        >>> query_profile_type.add_fields(
        ...     QueryTypeField(
        ...         name="age",
        ...         type="integer"
        ...     ),
        ...     QueryTypeField(
        ...         name="profession",
        ...         type="string"
        ...     )
        ... )
        """
        self.fields.extend(fields)

    @staticmethod
    def from_dict(mapping: Mapping) -> "QueryProfileType":
        return QueryProfileType(
            fields=[FromJson.map(field) for field in mapping.get("fields")]
        )

    @property
    def to_dict(self) -> Mapping:
        map = {"fields": [field.to_envelope for field in self.fields]}
        return map

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return self.fields == other.fields

    def __repr__(self):
        return "{0}({1})".format(
            self.__class__.__name__, repr(self.fields) if self.fields else None
        )


class QueryField(ToJson, FromJson["QueryField"]):
    def __init__(
        self,
        name: str,
        value: Union[str, int, float],
    ) -> None:
        """
        Create a field to be included in a :class:`QueryProfile`.

        :param name: Field name.
        :param value: Field value.

        >>> QueryField(name="maxHits", value=1000)
        QueryField('maxHits', 1000)
        """
        self.name = name
        self.value = value

    @staticmethod
    def from_dict(mapping: Mapping) -> "QueryField":
        return QueryField(
            name=mapping["name"],
            value=mapping["value"],
        )

    @property
    def to_dict(self) -> Mapping:
        map = {"name": self.name, "value": self.value}
        return map

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return self.name == other.name and self.value == other.value

    def __repr__(self):
        return "{0}({1}, {2})".format(
            self.__class__.__name__,
            repr(self.name),
            repr(self.value),
        )


class QueryProfile(ToJson, FromJson["QueryProfile"]):
    def __init__(self, fields: Optional[List[QueryField]] = None) -> None:
        """
        Create a Vespa Query Profile.

        Check the `Vespa documentation <https://docs.vespa.ai/documentation/query-profiles.html>`_
        for more detailed information about query profiles.

        :param fields: A list of :class:`QueryField`.

        >>> QueryProfile(fields=[QueryField(name="maxHits", value=1000)])
        QueryProfile([QueryField('maxHits', 1000)])
        """
        self.name = "default"
        self.type = "root"
        self.fields = [] if not fields else fields

    def add_fields(self, *fields: QueryField) -> None:
        """
        Add :class:`QueryField`'s to the Query Profile.

        :param fields: fields to be added

        >>> query_profile = QueryProfile()
        >>> query_profile.add_fields(QueryField(name="maxHits", value=1000))
        """
        self.fields.extend(fields)

    @staticmethod
    def from_dict(mapping: Mapping) -> "QueryProfile":
        return QueryProfile(
            fields=[FromJson.map(field) for field in mapping.get("fields")]
        )

    @property
    def to_dict(self) -> Mapping:
        map = {"fields": [field.to_envelope for field in self.fields]}
        return map

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return self.fields == other.fields

    def __repr__(self):
        return "{0}({1})".format(
            self.__class__.__name__, repr(self.fields) if self.fields else None
        )


class ApplicationPackage(ToJson, FromJson["ApplicationPackage"]):
    def __init__(
        self,
        name: str,
        schema: Optional[Schema] = None,
        query_profile: Optional[QueryProfile] = None,
        query_profile_type: Optional[QueryProfileType] = None,
    ) -> None:
        """
        Create a Vespa Application Package.

        Check the `Vespa documentation <https://docs.vespa.ai/documentation/cloudconfig/application-packages.html>`_
        for more detailed information about application packages.

        :param name: Application name.
        :param schema: :class:`Schema` of the application. If `None`, an empty :class:`Schema` with the same name of the
            application will be created by default.
        :param query_profile: :class:`QueryProfile` of the application. If `None`, a :class:`QueryProfile` named `default`
         with :class:`QueryProfileType` named `root` will be created by default.
        :param query_profile_type: :class:`QueryProfileType` of the application. If `None`, a empty
            :class:`QueryProfileType` named `root` will be created by default.

        The easiest way to get started is to create a default application package:

        >>> ApplicationPackage(name="test_app")
        ApplicationPackage('test_app', Schema('test_app', Document(None), None, None, []), QueryProfile(None), QueryProfileType(None))

        It will create a default :class:`Schema`, :class:`QueryProfile` and :class:`QueryProfileType` that you can then
        populate with specifics of your application.
        """
        self.name = name
        if not schema:
            schema = Schema(name=self.name, document=Document())
        self.schema = schema
        if not query_profile:
            query_profile = QueryProfile()
        self.query_profile = query_profile
        if not query_profile_type:
            query_profile_type = QueryProfileType()
        self.query_profile_type = query_profile_type
        self.model_ids = []
        self.model_configs = {}

    def add_model_ranking(
        self, model_config: ModelConfig, include_model_summary_features=False, **kwargs
    ) -> None:
        """
        Add ranking profile based on a specific model config.

        :param model_config: Model config instance specifying the model to be used on the RankProfile.
        :param include_model_summary_features: True to include model specific summary features, such as
            inputs and outputs that are useful for debugging. Default to False as this requires an extra model
            evaluation when fetching summary features.
        :param kwargs: Further arguments to be passed to RankProfile.
        :return: None
        """

        model_id = model_config.model_id
        #
        # Validate and persist config
        #
        if model_id in self.model_ids:
            raise ValueError("model_id must be unique: {}".format(model_id))
        self.model_ids.append(model_id)
        self.model_configs[model_id] = model_config

        if isinstance(model_config, BertModelConfig):
            self._add_bert_rank_profile(
                model_config=model_config,
                include_model_summary_features=include_model_summary_features,
                **kwargs
            )
        else:
            raise ValueError("Unknown model configuration type")

    def _add_bert_rank_profile(
        self,
        model_config: BertModelConfig,
        include_model_summary_features,
        doc_token_ids_indexing=None,
        **kwargs
    ) -> None:

        model_id = model_config.model_id

        #
        # export model
        #
        model_file_path = model_id + ".onnx"
        model_config.export_to_onnx(output_path=model_file_path)

        self.schema.add_model(
            OnnxModel(
                model_name=model_id,
                model_file_path=model_file_path,
                inputs={
                    "input_ids": "input_ids",
                    "token_type_ids": "token_type_ids",
                    "attention_mask": "attention_mask",
                },
                outputs={"output_0": "logits"},
            )
        )

        #
        # Add query profile type for query token ids
        #
        self.query_profile_type.add_fields(
            QueryTypeField(
                name="ranking.features.query({})".format(
                    model_config.query_token_ids_name
                ),
                type="tensor<float>(d0[{}])".format(
                    int(model_config.actual_query_input_size)
                ),
            )
        )

        #
        # Add field for doc token ids
        #
        if not doc_token_ids_indexing:
            doc_token_ids_indexing = ["attribute", "summary"]
        self.schema.add_fields(
            Field(
                name=model_config.doc_token_ids_name,
                type="tensor<float>(d0[{}])".format(
                    int(model_config.actual_doc_input_size)
                ),
                indexing=doc_token_ids_indexing,
            ),
        )

        #
        # Add rank profiles
        #
        constants = {"TOKEN_NONE": 0, "TOKEN_CLS": 101, "TOKEN_SEP": 102}
        if "contants" in kwargs:
            constants.update(kwargs.pop("contants"))

        functions = [
            Function(
                name="question_length",
                expression="sum(map(query({}), f(a)(a > 0)))".format(
                    model_config.query_token_ids_name
                ),
            ),
            Function(
                name="doc_length",
                expression="sum(map(attribute({}), f(a)(a > 0)))".format(
                    model_config.doc_token_ids_name
                ),
            ),
            Function(
                name="input_ids",
                expression="tokenInputIds({}, query({}), attribute({}))".format(
                    model_config.input_size,
                    model_config.query_token_ids_name,
                    model_config.doc_token_ids_name,
                ),
            ),
            Function(
                name="attention_mask",
                expression="tokenAttentionMask({}, query({}), attribute({}))".format(
                    model_config.input_size,
                    model_config.query_token_ids_name,
                    model_config.doc_token_ids_name,
                ),
            ),
            Function(
                name="token_type_ids",
                expression="tokenTypeIds({}, query({}), attribute({}))".format(
                    model_config.input_size,
                    model_config.query_token_ids_name,
                    model_config.doc_token_ids_name,
                ),
            ),
            Function(
                name="logit0",
                expression="onnx(" + model_id + ").logits{d0:0,d1:0}",
            ),
            Function(
                name="logit1",
                expression="onnx(" + model_id + ").logits{d0:0,d1:1}",
            ),
        ]
        if "functions" in kwargs:
            functions.extend(kwargs.pop("functions"))

        summary_features = []
        if include_model_summary_features:
            summary_features.extend(
                [
                    "logit0",
                    "logit1",
                    "input_ids",
                    "attention_mask",
                    "token_type_ids",
                ]
            )
        if "summary_features" in kwargs:
            summary_features.extend(kwargs.pop("summary_features"))

        self.schema.add_rank_profile(
            RankProfile(
                name=model_id,
                constants=constants,
                functions=functions,
                summary_features=summary_features,
                **kwargs
            )
        )

    @property
    def schema_to_text(self):
        env = Environment(
            loader=PackageLoader("vespa", "templates"),
            autoescape=select_autoescape(
                disabled_extensions=("txt",),
                default_for_string=True,
                default=True,
            ),
        )
        env.trim_blocks = True
        env.lstrip_blocks = True
        schema_template = env.get_template("schema.txt")
        return schema_template.render(
            schema_name=self.schema.name,
            document_name=self.schema.name,
            fields=self.schema.document.fields,
            fieldsets=self.schema.fieldsets,
            rank_profiles=self.schema.rank_profiles,
            models=self.schema.models,
        )

    @property
    def query_profile_to_text(self):
        env = Environment(
            loader=PackageLoader("vespa", "templates"),
            autoescape=select_autoescape(
                disabled_extensions=("txt",),
                default_for_string=True,
                default=True,
            ),
        )
        env.trim_blocks = True
        env.lstrip_blocks = True
        query_profile_template = env.get_template("query_profile.xml")
        return query_profile_template.render(fields=self.query_profile.fields)

    @property
    def query_profile_type_to_text(self):
        env = Environment(
            loader=PackageLoader("vespa", "templates"),
            autoescape=select_autoescape(
                disabled_extensions=("txt",),
                default_for_string=True,
                default=True,
            ),
        )
        env.trim_blocks = True
        env.lstrip_blocks = True
        query_profile_type_template = env.get_template("query_profile_type.xml")
        return query_profile_type_template.render(fields=self.query_profile_type.fields)

    @property
    def hosts_to_text(self):
        env = Environment(
            loader=PackageLoader("vespa", "templates"),
            autoescape=select_autoescape(
                disabled_extensions=("txt",),
                default_for_string=True,
                default=True,
            ),
        )
        env.trim_blocks = True
        env.lstrip_blocks = True
        schema_template = env.get_template("hosts.xml")
        return schema_template.render()

    @property
    def services_to_text(self):
        env = Environment(
            loader=PackageLoader("vespa", "templates"),
            autoescape=select_autoescape(
                disabled_extensions=("txt",),
                default_for_string=True,
                default=True,
            ),
        )
        env.trim_blocks = True
        env.lstrip_blocks = True
        schema_template = env.get_template("services.xml")
        return schema_template.render(
            application_name=self.name,
            document_name=self.schema.name,
        )

    @staticmethod
    def from_dict(mapping: Mapping) -> "ApplicationPackage":
        schema = mapping.get("schema", None)
        if schema is not None:
            schema = FromJson.map(schema)
        return ApplicationPackage(name=mapping["name"], schema=schema)

    @property
    def to_dict(self) -> Mapping:
        map = {"name": self.name}
        if self.schema is not None:
            map.update({"schema": self.schema.to_envelope})
        return map

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return self.name == other.name and self.schema == other.schema

    def __repr__(self):
        return "{0}({1}, {2}, {3}, {4})".format(
            self.__class__.__name__,
            repr(self.name),
            repr(self.schema),
            repr(self.query_profile),
            repr(self.query_profile_type),
        )


class VespaDocker(object):
    def __init__(
        self,
        port: int = 8080,
        output_file: IO = sys.stdout,
    ) -> None:
        """
        Manage Docker deployments.
        :param output_file: Output file to write output messages.
        """
        self.container = None
        self.local_port = port
        self.output = output_file

    def _run_vespa_engine_container(
        self,
        application_name: str,
        disk_folder: str,
        container_memory: str,
    ):
        client = docker.from_env()
        if self.container is None:
            try:
                self.container = client.containers.get(application_name)
            except docker.errors.NotFound:
                self.container = client.containers.run(
                    "vespaengine/vespa",
                    detach=True,
                    mem_limit=container_memory,
                    name=application_name,
                    hostname=application_name,
                    privileged=True,
                    volumes={disk_folder: {"bind": "/app", "mode": "rw"}},
                    ports={8080: self.local_port},
                )

    def _check_configuration_server(self) -> bool:
        """
        Check if configuration server is running and ready for deployment
        :return: True if configuration server is running.
        """
        return (
            self.container is not None
            and self.container.exec_run(
                "bash -c 'curl -s --head http://localhost:19071/ApplicationStatus'"
            )
            .output.decode("utf-8")
            .split("\r\n")[0]
            == "HTTP/1.1 200 OK"
        )

    @staticmethod
    def export_application_package(
        dir_path: str, application_package: ApplicationPackage
    ) -> None:
        """
        Export application package to disk.
        :param dir_path: Desired application path. Directory will be created if not already exist.
        :param application_package: Application package to export.
        :return: None. Application package file will be stored on `dir_path`.
        """
        Path(os.path.join(dir_path, "application/schemas")).mkdir(
            parents=True, exist_ok=True
        )
        with open(
            os.path.join(
                dir_path,
                "application/schemas/{}.sd".format(application_package.schema.name),
            ),
            "w",
        ) as f:
            f.write(application_package.schema_to_text)

        Path(os.path.join(dir_path, "application/search/query-profiles/types")).mkdir(
            parents=True, exist_ok=True
        )
        with open(
            os.path.join(
                dir_path,
                "application/search/query-profiles/default.xml",
            ),
            "w",
        ) as f:
            f.write(application_package.query_profile_to_text)
        with open(
            os.path.join(
                dir_path,
                "application/search/query-profiles/types/root.xml",
            ),
            "w",
        ) as f:
            f.write(application_package.query_profile_type_to_text)
        with open(os.path.join(dir_path, "application/hosts.xml"), "w") as f:
            f.write(application_package.hosts_to_text)
        with open(os.path.join(dir_path, "application/services.xml"), "w") as f:
            f.write(application_package.services_to_text)

        Path(os.path.join(dir_path, "application/files")).mkdir(
            parents=True, exist_ok=True
        )
        for model in application_package.schema.models:
            copyfile(
                model.model_file_path,
                os.path.join(dir_path, "application/files", model.model_file_name),
            )

    def _execute_deployment(
        self,
        application_name: str,
        disk_folder: str,
        container_memory: str = "4G",
    ):

        self._run_vespa_engine_container(
            application_name=application_name,
            disk_folder=disk_folder,
            container_memory=container_memory,
        )

        while not self._check_configuration_server():
            print("Waiting for configuration server.", file=self.output)
            sleep(5)

        deployment = self.container.exec_run(
            "bash -c '/opt/vespa/bin/vespa-deploy prepare /app/application && /opt/vespa/bin/vespa-deploy activate'"
        )

        deployment_message = deployment.output.decode("utf-8").split("\n")

        if not any(re.match("Generation: [0-9]+", line) for line in deployment_message):
            raise RuntimeError(deployment_message)

        app = Vespa(
            url="http://localhost",
            port=self.local_port,
            deployment_message=deployment_message,
        )

        while not app.get_application_status():
            print("Waiting for application status.", file=self.output)
            sleep(10)

        print("Finished deployment.", file=self.output)

        return app

    def deploy(
        self,
        application_package: ApplicationPackage,
        disk_folder: str,
        container_memory: str = "4G",
    ) -> Vespa:
        """
        Deploy the application package into a Vespa container.
        :param application_package: ApplicationPackage to be deployed.
        :param disk_folder: Disk folder to save the required Vespa config files.
        :param container_memory: Docker container memory available to the application.
        :return: a Vespa connection instance.
        """

        self.export_application_package(
            dir_path=disk_folder, application_package=application_package
        )

        return self._execute_deployment(
            application_name=application_package.name,
            disk_folder=disk_folder,
            container_memory=container_memory,
        )

    def deploy_from_disk(
        self,
        application_name: str,
        disk_folder: str,
        container_memory: str = "4G",
    ) -> Vespa:
        """
        Deploy disk-based application package into a Vespa container.
        :param application_name: Name of the application.
        :param disk_folder: Disk folder to save the required Vespa config files.
        :param container_memory: Docker container memory available to the application.
        :return: a Vespa connection instance.
        """

        return self._execute_deployment(
            application_name=application_name,
            disk_folder=disk_folder,
            container_memory=container_memory,
        )


class VespaCloud(object):
    def __init__(
        self,
        tenant: str,
        application: str,
        application_package: ApplicationPackage,
        key_location: Optional[str] = None,
        key_content: Optional[str] = None,
        output_file: IO = sys.stdout,
    ) -> None:
        """
        Deploy application to the Vespa Cloud (cloud.vespa.ai)

        :param tenant: Tenant name registered in the Vespa Cloud.
        :param application: Application name registered in the Vespa Cloud.
        :param application_package: ApplicationPackage to be deployed.
        :param key_location: Location of the private key used for signing HTTP requests to the Vespa Cloud.
        :param key_content: Content of the private key used for signing HTTP requests to the Vespa Cloud. Use only when
            key file is not available.
        :param output_file: Output file to write output messages.
        """
        self.tenant = tenant
        self.application = application
        self.application_package = application_package
        self.api_key = self._read_private_key(key_location, key_content)
        self.api_public_key_bytes = standard_b64encode(
            self.api_key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        self.data_key, self.data_certificate = self._create_certificate_pair()
        self.private_cert_file_name = "private_cert.txt"
        self.connection = http.client.HTTPSConnection(
            "api.vespa-external.aws.oath.cloud", 4443
        )
        self.output = output_file

    @staticmethod
    def _read_private_key(
        key_location: Optional[str] = None, key_content: Optional[str] = None
    ) -> ec.EllipticCurvePrivateKey:

        if key_content:
            key_content = bytes(key_content, "ascii")
        elif key_location:
            with open(key_location, "rb") as key_data:
                key_content = key_data.read()
        else:
            raise ValueError("Provide either key_content or key_location.")

        key = serialization.load_pem_private_key(key_content, None, default_backend())
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise TypeError("Key must be an elliptic curve private key")
        return key

    def _write_private_key_and_cert(
        self, key: ec.EllipticCurvePrivateKey, cert: x509.Certificate, disk_folder: str
    ) -> None:
        cert_file = os.path.join(disk_folder, self.private_cert_file_name)
        with open(cert_file, "w+") as file:
            file.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL,
                    serialization.NoEncryption(),
                ).decode("UTF-8")
            )
            file.write(cert.public_bytes(serialization.Encoding.PEM).decode("UTF-8"))

    @staticmethod
    def _create_certificate_pair() -> (ec.EllipticCurvePrivateKey, x509.Certificate):
        key = ec.generate_private_key(ec.SECP384R1, default_backend())
        name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, u"localhost")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.utcnow() - timedelta(minutes=1))
            .not_valid_after(datetime.utcnow() + timedelta(days=7))
            .public_key(key.public_key())
            .sign(key, hashes.SHA256(), default_backend())
        )
        return (key, certificate)

    def _request(
        self, method: str, path: str, body: BytesIO = BytesIO(), headers={}
    ) -> dict:
        digest = hashes.Hash(hashes.SHA256(), default_backend())
        body.seek(0)
        digest.update(body.read())
        content_hash = standard_b64encode(digest.finalize()).decode("UTF-8")
        timestamp = (
            datetime.utcnow().isoformat() + "Z"
        )  # Java's Instant.parse requires the neutral time zone appended
        url = "https://" + self.connection.host + ":" + str(self.connection.port) + path

        canonical_message = method + "\n" + url + "\n" + timestamp + "\n" + content_hash
        signature = self.api_key.sign(
            canonical_message.encode("UTF-8"), ec.ECDSA(hashes.SHA256())
        )

        headers = {
            "X-Timestamp": timestamp,
            "X-Content-Hash": content_hash,
            "X-Key-Id": self.tenant + ":" + self.application + ":" + "default",
            "X-Key": self.api_public_key_bytes,
            "X-Authorization": standard_b64encode(signature),
            **headers,
        }

        body.seek(0)
        self.connection.request(method, path, body, headers)
        with self.connection.getresponse() as response:
            parsed = json.load(response)
            if response.status != 200:
                raise RuntimeError(
                    "Status code "
                    + str(response.status)
                    + " doing "
                    + method
                    + " at "
                    + url
                    + ":\n"
                    + parsed["message"]
                )
            return parsed

    def _get_dev_region(self) -> str:
        return self._request("GET", "/zone/v1/environment/dev/default")["name"]

    def _get_endpoint(self, instance: str, region: str) -> str:
        endpoints = self._request(
            "GET",
            "/application/v4/tenant/{}/application/{}/instance/{}/environment/dev/region/{}".format(
                self.tenant, self.application, instance, region
            ),
        )["endpoints"]
        container_url = [
            endpoint["url"]
            for endpoint in endpoints
            if endpoint["cluster"]
            == "{}_container".format(self.application_package.name)
        ]
        if not container_url:
            raise RuntimeError("No endpoints found for container 'test_app_container'")
        return container_url[0]

    def _to_application_zip(self) -> BytesIO:
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "a") as zip_archive:
            zip_archive.writestr(
                "application/schemas/{}.sd".format(
                    self.application_package.schema.name
                ),
                self.application_package.schema_to_text,
            )
            zip_archive.writestr(
                "application/search/query-profiles/default.xml",
                self.application_package.query_profile_to_text,
            )
            zip_archive.writestr(
                "application/search/query-profiles/types/root.xml",
                self.application_package.query_profile_type_to_text,
            )
            zip_archive.writestr(
                "application/services.xml", self.application_package.services_to_text
            )
            zip_archive.writestr(
                "application/security/clients.pem",
                self.data_certificate.public_bytes(serialization.Encoding.PEM),
            )
            for model in self.application_package.schema.models:
                zip_archive.write(
                    model.model_file_path,
                    os.path.join("application/files", model.model_file_name),
                )

        return buffer

    def _start_deployment(self, instance: str, job: str, disk_folder: str) -> int:
        deploy_path = (
            "/application/v4/tenant/{}/application/{}/instance/{}/deploy/{}".format(
                self.tenant, self.application, instance, job
            )
        )

        application_zip_bytes = self._to_application_zip()

        Path(disk_folder).mkdir(parents=True, exist_ok=True)

        self._write_private_key_and_cert(
            self.data_key, self.data_certificate, disk_folder
        )

        response = self._request(
            "POST",
            deploy_path,
            application_zip_bytes,
            {"Content-Type": "application/zip"},
        )
        print(response["message"], file=self.output)
        return response["run"]

    def _get_deployment_status(
        self, instance: str, job: str, run: int, last: int
    ) -> (str, int):

        update = self._request(
            "GET",
            "/application/v4/tenant/{}/application/{}/instance/{}/job/{}/run/{}?after={}".format(
                self.tenant, self.application, instance, job, run, last
            ),
        )

        for step, entries in update["log"].items():
            for entry in entries:
                self._print_log_entry(step, entry)
        last = update.get("lastId", last)

        fail_status_message = {
            "error": "Unexpected error during deployment; see log for details",
            "aborted": "Deployment was aborted, probably by a newer deployment",
            "outOfCapacity": "No capacity left in zone; please contact the Vespa team",
            "deploymentFailed": "Deployment failed; see log for details",
            "installationFailed": "Installation failed; see Vespa log for details",
            "running": "Deployment not completed",
            "endpointCertificateTimeout": "Endpoint certificate not ready in time; please contact Vespa team",
            "testFailure": "Unexpected status; tests are not run for manual deployments",
        }

        if update["active"]:
            return "active", last
        else:
            status = update["status"]
            if status == "success":
                return "success", last
            elif status in fail_status_message.keys():
                raise RuntimeError(fail_status_message[status])
            else:
                raise RuntimeError("Unexpected status: {}".format(status))

    def _follow_deployment(self, instance: str, job: str, run: int) -> None:
        last = -1
        while True:
            try:
                status, last = self._get_deployment_status(instance, job, run, last)
            except RuntimeError:
                raise

            if status == "active":
                sleep(1)
            elif status == "success":
                return
            else:
                raise RuntimeError("Unexpected status: {}".format(status))

    def _print_log_entry(self, step: str, entry: dict):
        timestamp = strftime("%H:%M:%S", gmtime(entry["at"] / 1e3))
        message = entry["message"].replace("\n", "\n" + " " * 23)
        if step != "copyVespaLogs" or entry["type"] == "error":
            print(
                "{:<7} [{}]  {}".format(entry["type"].upper(), timestamp, message),
                file=self.output,
            )

    def deploy(self, instance: str, disk_folder: str) -> Vespa:
        """
        Deploy the given application package as the given instance in the Vespa Cloud dev environment.

        :param instance: Name of this instance of the application, in the Vespa Cloud.
        :param disk_folder: Disk folder to save the required Vespa config files.

        :return: a Vespa connection instance.
        """
        region = self._get_dev_region()
        job = "dev-" + region
        run = self._start_deployment(instance, job, disk_folder)
        self._follow_deployment(instance, job, run)
        endpoint_url = self._get_endpoint(instance=instance, region=region)
        print("Finished deployment.", file=self.output)
        return Vespa(
            url=endpoint_url,
            cert=os.path.join(disk_folder, self.private_cert_file_name),
        )

    def delete(self, instance: str):
        """
        Delete the specified instance from the dev environment in the Vespa Cloud.
        :param instance: Name of the instance to delete.
        :return:
        """
        print(
            self._request(
                "DELETE",
                "/application/v4/tenant/{}/application/{}/instance/{}/environment/dev/region/{}".format(
                    self.tenant, self.application, instance, self._get_dev_region()
                ),
            )["message"],
            file=self.output,
        )
        print(
            self._request(
                "DELETE",
                "/application/v4/tenant/{}/application/{}/instance/{}".format(
                    self.tenant, self.application, instance
                ),
            )["message"],
            file=self.output,
        )

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
