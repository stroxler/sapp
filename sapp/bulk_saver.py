# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Bulk saving objects for performance
"""

import logging
from typing import Any, Dict, Iterator, List, Optional, TypeVar

from .db import DB
from .decorators import log_time
from .iterutil import split_every
from .models import (
    Feature,
    Issue,
    IssueInstance,
    IssueInstanceFeatureAssoc,
    IssueInstanceFixInfo,
    IssueInstanceSharedTextAssoc,
    IssueInstanceTraceFrameAssoc,
    PrimaryKeyGenerator,
    SharedText,
    TraceFrame,
    TraceFrameAnnotation,
    TraceFrameAnnotationTraceFrameAssoc,
    TraceFrameLeafAssoc,
)

log: logging.Logger = logging.getLogger("sapp")


class BulkSaver:
    """Stores new objects created within a run and bulk save them"""

    # order is significant, objects will be saved in this order.
    SAVING_CLASSES_ORDER = [
        SharedText,
        Issue,
        IssueInstanceFixInfo,
        IssueInstance,
        IssueInstanceSharedTextAssoc,
        TraceFrame,
        IssueInstanceTraceFrameAssoc,
        TraceFrameAnnotation,
        TraceFrameLeafAssoc,
        TraceFrameAnnotationTraceFrameAssoc,
        Feature,
        IssueInstanceFeatureAssoc,
    ]

    BATCH_SIZE = 30000

    def __init__(
        self, primary_key_generator: Optional[PrimaryKeyGenerator] = None
    ) -> None:
        self.primary_key_generator: PrimaryKeyGenerator = (
            primary_key_generator or PrimaryKeyGenerator()
        )
        self.saving: Dict[str, Any] = {}
        for cls in self.SAVING_CLASSES_ORDER:
            self.saving[cls.__name__] = []

    # pyre-fixme[2]: Parameter must be annotated.
    def add(self, item) -> None:
        assert item.model in self.SAVING_CLASSES_ORDER, (
            "%s should be added with session.add()" % item.model.__name__
        )
        self.saving[item.model.__name__].append(item)

    # pyre-fixme[2]: Parameter must be annotated.
    def add_all(self, items) -> None:
        if items:
            assert items[0].model in self.SAVING_CLASSES_ORDER, (
                "%s should be added with session.add_all()" % items[0].model.__name__
            )
            self.saving[items[0].model.__name__].extend(items)

    # pyre-fixme[3]: Return type must be annotated.
    # pyre-fixme[2]: Parameter must be annotated.
    def get_items_to_add(self, cls):
        return self.saving[cls.__name__]

    def save_all(self, database: DB) -> None:
        saving_classes = [
            cls
            for cls in self.SAVING_CLASSES_ORDER
            if len(self.saving[cls.__name__]) != 0
        ]

        item_counts = {
            cls.__name__: len(self.get_items_to_add(cls)) for cls in saving_classes
        }

        with database.make_session() as session:
            pk_gen = self.primary_key_generator.reserve(
                session, saving_classes, item_counts
            )

        for cls in saving_classes:
            log.info("Saving %s...", cls.__name__)
            self._save(database, cls, pk_gen)

    @log_time
    # pyre-fixme[2]: Parameter must be annotated.
    def _save(self, database: DB, cls, pk_gen: PrimaryKeyGenerator) -> None:
        # We sort keys because bulk insert uses executemany, but it can only
        # group together sequential items with the same keys. If we are scattered
        # then it does far more executemany calls, and it kills performance.
        items = sorted(
            cls.prepare(database, pk_gen, consume(self.saving[cls.__name__])),
            key=lambda k: list(k.keys()),
        )

        # bulk_insert_mappings should only be used for new objects.
        # To update an existing object, just modify its attribute(s)
        # and call session.commit()
        for group in split_every(self.BATCH_SIZE, items):
            with database.make_session() as session:
                session.bulk_insert_mappings(cls, group, render_nulls=True)
                session.commit()

    def add_trace_frame_leaf_assoc(
        self, message: SharedText, trace_frame: TraceFrame, depth: Optional[int]
    ) -> None:
        self.add(
            TraceFrameLeafAssoc.Record(
                trace_frame_id=trace_frame.id, leaf_id=message.id, trace_length=depth
            )
        )

    def add_issue_instance_trace_frame_assoc(
        self, issue_instance: IssueInstance, trace_frame: TraceFrame
    ) -> None:
        self.add(
            IssueInstanceTraceFrameAssoc.Record(
                issue_instance_id=issue_instance.id, trace_frame_id=trace_frame.id
            )
        )

    def add_issue_instance_shared_text_assoc(
        self, issue_instance: IssueInstance, shared_text: SharedText
    ) -> None:
        self.add(
            IssueInstanceSharedTextAssoc.Record(
                issue_instance_id=issue_instance.id, shared_text_id=shared_text.id
            )
        )

    # pyre-fixme[2]: Parameter must be annotated.
    # pyre-fixme[2]: Parameter must be annotated.
    def add_issue_instance_feature_assoc(self, issue_instance, feature) -> None:
        self.add(
            IssueInstanceFeatureAssoc.Record(
                issue_instance_id=issue_instance.id, feature_id=feature.id
            )
        )

    def add_trace_frame_annotation_trace_frame_assoc(
        self,
        trace_frame_annotation: TraceFrameAnnotation,
        trace_frame: TraceFrame,
    ) -> None:
        self.add(
            TraceFrameAnnotationTraceFrameAssoc.Record(
                trace_frame_annotation_id=trace_frame_annotation.id,
                trace_frame_id=trace_frame.id,
            )
        )

    def dump_stats(self) -> str:
        stat_str = ""
        for cls in self.SAVING_CLASSES_ORDER:
            stat_str += "%s: %d\n" % (cls.__name__, len(self.saving[cls.__name__]))
        return stat_str


T = TypeVar("T")


def consume(lst: List[T]) -> Iterator[T]:
    while len(lst) > 0:
        yield lst.pop()
