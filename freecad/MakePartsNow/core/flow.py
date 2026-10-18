"""The work each panel action does on the worker thread.

Each function takes a ``CadClient`` and a ``worker.Job`` and returns JSON data
for the main thread. None of them touch a CAD program's API. ``run_new_quote``
removes the export folder when it finishes, whether it succeeded or failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence, Union

from . import export
from .text import is_allowed_link
from .client import CadClient, CadError, QuoteItem, QuoteView, requote_items, to_jsonable
from .worker import Job, retry

CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class UploadFile:
    path: str
    file_name: str
    quantity: int
    finish: str
    units: Optional[str] = None  # "mm" for STL
    material: Optional[str] = None  # None means the server's default, PA12


@dataclass(frozen=True)
class KnownPart:
    """A line whose file is already on the server (reused after a fix elsewhere, or a requote)."""

    part_id: str
    quantity: int
    finish: str
    material: Optional[str] = None


Line = Union[UploadFile, KnownPart]


def _check_cancel(job: Job) -> None:
    if job.cancelled:
        raise CadError(CANCELLED, "Stopped.", transient=False)


def _wait(client: CadClient, job: Job, view: QuoteView, items: Sequence[QuoteItem], ship_to_zip: str, max_wait: float) -> QuoteView:
    def progress(current: QuoteView, elapsed: float) -> None:
        _check_cancel(job)
        pending = sum(1 for p in current.parts if p.status == "processing")
        job.progress(f"Analyzing {pending} of {len(current.parts)} part(s)... {int(elapsed)} s")  # phase "analyze"

    return client.wait_for_quote(view, items, ship_to_zip, max_wait=max_wait, progress=progress)


def fetch_options(client: CadClient, job: Job) -> dict[str, Any]:
    return to_jsonable(retry(client.get_options, should_stop=lambda: job.cancelled))


def run_new_quote(
    client: CadClient,
    job: Job,
    lines: Sequence[Line],
    ship_to_zip: str,
    temp_dir: Optional[str],
    *,
    max_wait: float,
    read_file: Callable[[str], bytes] = lambda p: open(p, "rb").read(),
) -> dict[str, Any]:
    """Upload the new files (one link each), quote every line as one order, wait for analysis.

    Lines that are ``KnownPart`` reuse a file already uploaded. The answer lists parts
    in the order of ``lines``.
    """
    try:
        _check_cancel(job)
        files = [line for line in lines if isinstance(line, UploadFile)]
        upload_ids: dict[int, str] = {}
        if files:
            job.progress("Preparing upload...")
            links = retry(lambda: client.create_upload_links(len(files), [f.file_name for f in files]), should_stop=lambda: job.cancelled)
            for index, (item, link) in enumerate(zip(files, links), start=1):
                _check_cancel(job)
                job.progress(f"Uploading {index} of {len(files)}: {item.file_name}")
                data = read_file(item.path)
                retry(lambda: client.put_file(link, data, item.file_name), should_stop=lambda: job.cancelled)
                upload_ids[id(item)] = link.upload_id
        _check_cancel(job)
        job.progress("Analyzing your parts...")
        items = [
            QuoteItem(quantity=line.quantity, finish=line.finish, upload_id=upload_ids[id(line)], units=line.units, material=line.material)
            if isinstance(line, UploadFile)
            else QuoteItem(quantity=line.quantity, finish=line.finish, part_id=line.part_id, material=line.material)
            for line in lines
        ]
        view = retry(lambda: client.quote_parts(items, ship_to_zip), should_stop=lambda: job.cancelled)
        view = _wait(client, job, view, items, ship_to_zip, max_wait)
        return to_jsonable(view)
    finally:
        export.cleanup(temp_dir)


def run_update_quote(
    client: CadClient,
    job: Job,
    quote_id: str,
    items: Sequence[QuoteItem],
    ship_to_zip: str,
    *,
    max_wait: float,
) -> dict[str, Any]:
    """Revise a quote with new quantities, finishes or ZIP, reusing the uploaded parts."""
    job.progress("Pricing your parts...")
    view = retry(lambda: client.update_quote(quote_id, items, ship_to_zip), should_stop=lambda: job.cancelled)
    if view.is_processing:
        view = _wait(client, job, view, requote_items(view, items), ship_to_zip, max_wait)
    return to_jsonable(view)


def run_checkout(client: CadClient, job: Job, quote_id: str, open_url: Callable[[str], bool]) -> dict[str, Any]:
    """Make a checkout link and open it in the browser, only when its status is ``ready``.

    If the quote was revised meanwhile (QUOTE_REVISED), fetch the newest revision so the
    palette can show its prices before the user orders.
    """
    job.progress("Opening checkout...")
    checkout = retry(lambda: client.create_checkout_link(quote_id), should_stop=lambda: job.cancelled)
    opened = False
    url = checkout.checkout_url or ""
    # Only links the panels may open anyway: https on makepartsnow.com, or the configured dev server. A prefix test
    # ("http://localhost") would also pass http://localhost.example.com.
    if checkout.status == "ready" and is_allowed_link(url, client.base_url):
        try:
            opened = bool(open_url(url))
        except Exception:  # noqa: BLE001 - the link is shown in the palette either way
            opened = False
    result: dict[str, Any] = {"checkout": to_jsonable(checkout), "opened": opened, "latest_quote": None}
    latest = checkout.latest_quote_id
    if latest:
        result["latest_quote"] = to_jsonable(retry(lambda: client.get_quote(latest), should_stop=lambda: job.cancelled))
    return result


def run_delete(client: CadClient, job: Job, parts: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """Delete uploaded files with their delete tokens."""
    job.progress("Deleting your uploaded files...")
    return to_jsonable(retry(lambda: client.delete_parts(parts), should_stop=lambda: job.cancelled))
