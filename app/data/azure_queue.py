"""Service Bus adapter for the ingest queue.

The client, sender, and receiver are created once and held open for the
adapter's lifetime. That is required, not an optimization: a Service Bus
message lock belongs to the receiver that fetched it, so settling through a
receiver that has since been closed fails. `AzureServiceBusQueue.aclose()` is
what finally releases them.
"""

from __future__ import annotations

import logging

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import ServiceBusMessage
from azure.servicebus.aio import ServiceBusClient

logger = logging.getLogger(__name__)

# How long to wait for a message before concluding the queue is empty. The job
# is started by KEDA because messages already exist, so this only governs the
# tail of a drain; long values just burn execution time on an empty queue.
RECEIVE_WAIT_SECONDS = 5


class AzureServiceBusQueue:
    def __init__(self, namespace: str, queue_name: str = "contract-ingest"):
        self._credential = DefaultAzureCredential()
        self._client = ServiceBusClient(
            fully_qualified_namespace=namespace, credential=self._credential
        )
        self._queue_name = queue_name
        self._sender = None
        self._receiver = None

    async def enqueue(self, contract_id: str) -> None:
        if self._sender is None:
            self._sender = self._client.get_queue_sender(queue_name=self._queue_name)
        # The body is just the contract id. Everything else about the contract
        # already lives in Cosmos, and a fat message body is a second copy of
        # state that can disagree with the first.
        await self._sender.send_messages(ServiceBusMessage(contract_id))

    async def receive(self, max_messages: int = 1) -> list[_ServiceBusLease]:
        if self._receiver is None:
            self._receiver = self._client.get_queue_receiver(queue_name=self._queue_name)
        messages = await self._receiver.receive_messages(
            max_message_count=max_messages, max_wait_time=RECEIVE_WAIT_SECONDS
        )
        return [_ServiceBusLease(m, self._receiver) for m in messages]

    async def aclose(self) -> None:
        if self._sender is not None:
            await self._sender.close()
        if self._receiver is not None:
            await self._receiver.close()
        await self._client.close()
        await self._credential.close()


class _ServiceBusLease:
    """One message, still locked to the receiver that fetched it."""

    __slots__ = ("_message", "_receiver")

    def __init__(self, message, receiver):
        self._message = message
        self._receiver = receiver

    @property
    def contract_id(self) -> str:
        return str(self._message)

    async def complete(self) -> None:
        await self._receiver.complete_message(self._message)

    async def abandon(self) -> None:
        """Release the lock for redelivery.

        Abandon rather than dead-letter even on a permanent failure: telling a
        transient Document Intelligence 503 apart from a scanned PDF that will
        never parse takes error classification this doesn't have yet, so the
        queue's own maxDeliveryCount (5, set in Bicep) makes the call. The cost
        is that a genuinely poison document is retried four more times before
        dead-lettering — cheap, and the contract row is already marked FAILED
        with a reason after the first attempt, so nothing is hidden meanwhile.
        """
        await self._receiver.abandon_message(self._message)
