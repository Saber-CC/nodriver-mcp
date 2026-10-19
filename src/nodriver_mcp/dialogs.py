"""Connection-scoped dialog observation and explicit handling policies."""

import itertools
import weakref
from dataclasses import dataclass, field

from nodriver import cdp


def dialog_type(event):
    return getattr(event.type_, "value", event.type_)


@dataclass
class DialogRecord:
    id: int
    page_id: str
    event: object
    connection: object
    owners: set[int] = field(default_factory=set)
    action: str | None = None
    prompt_text: str = ""
    closed: bool = False
    error: str | None = None

    def data(self):
        return {
            "type": dialog_type(self.event),
            "message": self.event.message,
            "default_prompt": getattr(self.event, "default_prompt", None),
            "action": self.action,
            "closed": self.closed,
            "error": self.error,
        }


class DialogInterrupted(ValueError):
    def __init__(self, record):
        self.record = record
        event = record.event
        reason = f" Automatic handling failed: {record.error}." if record.error else ""
        super().__init__(
            f"Action interrupted by {dialog_type(event)} dialog: {event.message}.{reason} "
            "Use handle_dialog on the same page_id."
        )


class DialogRegistry:
    def __init__(self):
        self.current = {}
        self.watchers = {}
        self.ids = itertools.count(1)

    def opened(self, page_id, connection, event):
        record = self.current.get(page_id)
        if (
            record is not None
            and not record.closed
            and (
                dialog_type(record.event),
                record.event.message,
                getattr(record.event, "url", None),
            )
            == (dialog_type(event), event.message, getattr(event, "url", None))
        ):
            record.owners.add(id(connection))
            return record
        record = DialogRecord(
            next(self.ids), page_id, event, weakref.ref(connection), {id(connection)}
        )
        self.current[page_id] = record
        return record

    def closed(self, page_id, connection):
        record = self.current.get(page_id)
        if record is not None and id(connection) in record.owners:
            record.closed = True
            self.current.pop(page_id, None)

    def watch(self, connection, page_id):
        key = id(connection)
        existing = self.watchers.get(key)
        if existing is not None and existing[0]() is connection:
            return
        reference = weakref.ref(connection, lambda _ref: self.forget_key(key))

        def opened(event, _connection=None):
            owner = reference()
            if owner is not None:
                self.opened(page_id, owner, event)

        def closed(_event, _connection=None):
            owner = reference()
            if owner is not None:
                self.closed(page_id, owner)

        callbacks = [
            (cdp.page.JavascriptDialogOpening, opened),
            (cdp.page.JavascriptDialogClosed, closed),
        ]
        for event, callback in callbacks:
            connection.add_handler(event, callback)
        self.watchers[key] = (reference, page_id, callbacks)

    def forget_key(self, key):
        entry = self.watchers.pop(key, None)
        if entry is None:
            return
        reference, page_id, callbacks = entry
        connection = reference()
        if connection is not None:
            for event, callback in callbacks:
                connection.remove_handler(event, callback)
        record = self.current.get(page_id)
        if record is not None:
            record.owners.discard(key)
            if not record.owners:
                self.current.pop(page_id, None)

    def forget(self, connection):
        self.forget_key(id(connection))

    def clear(self):
        for key in list(self.watchers):
            self.forget_key(key)
        self.current.clear()

    async def handle(self, record, action, prompt_text="", fallback=None):
        if record.closed:
            return
        connection = record.connection() or fallback
        if connection is None:
            raise ValueError("Dialog connection has closed.")
        record.action, record.prompt_text = action, prompt_text
        try:
            await connection.send(
                cdp.page.handle_java_script_dialog(
                    accept=action == "accept",
                    prompt_text=prompt_text if action == "accept" else None,
                )
            )
        except Exception:
            if not record.closed:
                raise
            record.action = None
        record.closed = True
        if self.current.get(record.page_id) is record:
            self.current.pop(record.page_id, None)
