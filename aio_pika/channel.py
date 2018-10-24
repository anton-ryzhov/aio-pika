import asyncio
from typing import Callable, Any, Union, Awaitable

from logging import getLogger
from types import FunctionType

from aio_pika.pika.spec import BasicProperties, Channel as PikaChannel
from . import exceptions
from .common import BaseChannel, FutureStore, ConfirmationTypes
from .exchange import Exchange, ExchangeType
from .message import IncomingMessage, ReturnedMessage
from .queue import Queue
from .transaction import Transaction


log = getLogger(__name__)

FunctionOrCoroutine = Union[
    Callable[[IncomingMessage], Any],
    Awaitable[IncomingMessage]
]


class Channel(BaseChannel):
    """ Channel abstraction """

    QUEUE_CLASS = Queue
    EXCHANGE_CLASS = Exchange

    __slots__ = ('_connection', '__closing', '_confirmations', '_delivery_tag',
                 'loop', '_futures', '_channel', '_on_return_callbacks',
                 'default_exchange', '_write_lock', '_channel_number',
                 '_publisher_confirms', '_on_return_raises')

    def __init__(self, connection, loop: asyncio.AbstractEventLoop,
                 future_store: FutureStore, channel_number: int=None,
                 publisher_confirms: bool=True, on_return_raises=False):
        """

        :param connection: :class:`aio_pika.adapter.AsyncioConnection` instance
        :param loop: Event loop (:func:`asyncio.get_event_loop()`
                when :class:`None`)
        :param future_store: :class:`aio_pika.common.FutureStore` instance
        :param publisher_confirms: False if you don't need delivery
                confirmations (in pursuit of performance)
        """
        super().__init__(loop, future_store.get_child())

        self._channel = None  # type: pika.channel.Channel
        self._connection = connection
        self._confirmations = {}
        self._on_return_callbacks = []
        self._delivery_tag = 0
        self._write_lock = asyncio.Lock(loop=self.loop)
        self._channel_number = channel_number
        self._publisher_confirms = publisher_confirms

        if not publisher_confirms and on_return_raises:
            raise RuntimeError(
                'on_return_raises must be uses with publisher confirms'
            )

        self._on_return_raises = on_return_raises

        self.default_exchange = self.EXCHANGE_CLASS(
            self._channel,
            self._publish,
            '',
            ExchangeType.DIRECT,
            durable=None,
            auto_delete=None,
            internal=None,
            passive=None,
            arguments=None,
            loop=self.loop,
            future_store=self._futures.get_child(),
        )

    @property
    def _channel_maker(self):
        return self._connection._connection.channel

    @property
    def number(self):
        return self._channel.channel_number

    def __str__(self):
        return "{0}".format(
            self.number if self._channel else "Not initialized channel"
        )

    def __repr__(self):
        return '<%s "%s#%s">' % (
            self.__class__.__name__, self._connection, self
        )

    def __iter__(self):
        return (yield from self.__await__())

    def __await__(self):
        yield from self.initialize().__await__()
        return self

    async def __aenter__(self):
        if not self._channel:
            await self.initialize()

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _on_channel_close(self, channel: PikaChannel, code: int, reason):
        # We have to check if the channel has already been closed, as this
        # could have been done within `_publish` method
        if self.is_closed:
            return
        # In case of normal closing, closing code should be unaltered
        # (0 by default)
        # See: https://github.com/pika/pika/blob/8d970e1/pika/channel.py#L84
        exc = exceptions.ChannelClosed(code, reason)

        if code == 0:
            self._closing.set_result(None)
            log_method = log.debug
        else:
            self._closing.set_exception(exc)
            log_method = log.error

        log_method("Channel %r closed: %d - %s", channel, code, reason)

        self._futures.reject_all(exc)
        return exc

    def _on_return(self, channel, message, properties, body):
        msg = ReturnedMessage(
            body=body,
            channel=channel,
            envelope=message,
            properties=properties,
        )

        for callback in self._on_return_callbacks:
            self.loop.create_task(callback(msg))

    def add_close_callback(self, callback: FunctionType) -> None:
        self._closing.add_done_callback(lambda r: callback(r))

    def remove_close_callback(self, callback: FunctionType) -> None:
        self._closing.remove_done_callback(callback)

    @property
    async def closing(self):
        """ Return future which will be finished after channel close. """
        return await self._closing

    def add_on_return_callback(self, callback: FunctionOrCoroutine) -> None:
        self._on_return_callbacks.append(asyncio.coroutine(callback))

    async def _create_channel(self, timeout=None):
        future = self._create_future(timeout=timeout)

        self._channel_maker(
            future.set_result,
            channel_number=self._channel_number
        )

        channel = await future  # type: pika.channel.Channel

        if self._publisher_confirms:
            channel.confirm_delivery(self._on_delivery_confirmation)

            if self._on_return_raises:
                channel.add_on_return_callback(self._on_return_delivery)

        channel.add_on_close_callback(self._on_channel_close)
        channel.add_on_return_callback(self._on_return)

        return channel

    async def initialize(self, timeout=None) -> None:
        async with self._write_lock:
            if self._closing.done():
                raise RuntimeError("Can't initialize closed channel")

            self._channel = await self._create_channel(timeout)
            self._connection._on_channel_open(self)
            self._delivery_tag = 0

    def _on_return_delivery(self, channel, method_frame, properties, body):
        f = self._confirmations.pop(int(properties.headers.get('delivery-tag')))
        f.set_exception(exceptions.UnroutableError([body]))

    def _on_delivery_confirmation(self, method_frame):
        future = self._confirmations.pop(
            method_frame.method.delivery_tag, None
        )

        if not future:
            log.info(
                "Unknown delivery tag %d for message confirmation \"%s\"",
                method_frame.method.delivery_tag, method_frame.method.NAME
            )
            return

        try:
            confirmation_type = ConfirmationTypes(
                method_frame.method.NAME.split('.')[1].lower()
            )

            if confirmation_type == ConfirmationTypes.ACK:
                future.set_result(True)
            elif confirmation_type == ConfirmationTypes.NACK:
                future.set_exception(exceptions.DeliveryError(method_frame))
        except ValueError:
            future.set_exception(
                RuntimeError('Unknown method frame', method_frame)
            )
        except Exception as e:
            future.set_exception(e)

    @BaseChannel._ensure_channel_is_open
    async def declare_exchange(self, name: str,
                               type: ExchangeType=ExchangeType.DIRECT,
                               durable: bool=None, auto_delete: bool=False,
                               internal: bool=False, passive: bool=False,
                               arguments: dict=None,
                               timeout: int=None) -> Exchange:

        async with self._write_lock:
            if auto_delete and durable is None:
                durable = False

            exchange = self.EXCHANGE_CLASS(
                self._channel, self._publish, name, type,
                durable=durable, auto_delete=auto_delete, internal=internal,
                passive=passive, arguments=arguments, loop=self.loop,
                future_store=self._futures.get_child(),
            )

            await exchange.declare(timeout=timeout)

            log.debug("Exchange declared %r", exchange)

            return exchange

    @BaseChannel._ensure_channel_is_open
    async def _publish(self, queue_name, routing_key, body,
                       properties: BasicProperties, mandatory, immediate):

        while self._connection.is_closed:
            log.debug(
                "Can't publish message because connection is inactive"
            )
            await asyncio.sleep(1, loop=self.loop)

        async with self._write_lock:

            f = self._create_future()
            self._delivery_tag += 1

            if self._on_return_raises:
                properties.headers = properties.headers or {}
                properties.headers['delivery-tag'] = str(self._delivery_tag)

            try:
                self._channel.basic_publish(
                    queue_name, routing_key, body,
                    properties, mandatory, immediate
                )
            except (AttributeError, RuntimeError) as exc:
                log.exception(
                    "Failed to send data to client "
                    "(connection unexpectedly closed)"
                )
                self._on_channel_close(self._channel, -1, exc)
                self._connection._connection.close(
                    reply_code=500, reply_text="Incorrect state"
                )
            else:
                if self._publisher_confirms:
                    self._confirmations[self._delivery_tag] = f
                else:
                    f.set_result(None)

            return await f

    @BaseChannel._ensure_channel_is_open
    async def declare_queue(self, name: str=None, *, durable: bool=None,
                            exclusive: bool=False, passive: bool=False,
                            auto_delete: bool=False, arguments: dict=None,
                            timeout: int=None) -> Queue:
        """

        :param name: queue name
        :param durable: Durability (queue survive broker restart)
        :param exclusive: Makes this queue exclusive. Exclusive queues may only
            be accessed by the current connection, and are deleted when that
            connection closes. Passive declaration of an exclusive queue by
            other connections are not allowed.
        :param passive: Only check to see if the queue exists.
        :param auto_delete: Delete queue when channel will be closed.
        :param arguments: pika additional arguments
        :param timeout: execution timeout
        :return: :class:`aio_pika.queue.Queue` instance
        """

        async with self._write_lock:
            if auto_delete and durable is None:
                durable = False

            queue = self.QUEUE_CLASS(
                self.loop, self._futures.get_child(), self._channel, name,
                durable, exclusive, auto_delete, arguments,
                passive=passive
            )

            await queue.declare(timeout)
            return queue

    async def close(self) -> None:
        if not self._channel:
            log.warning("Channel already closed")
            return

        async with self._write_lock:
            self._channel.close()
            await self.closing
            self._channel = None

    @BaseChannel._ensure_channel_is_open
    async def set_qos(self, prefetch_count: int=0, prefetch_size: int=0,
                      all_channels=False, timeout: int=None):
        async with self._write_lock:
            f = self._create_future(timeout=timeout)

            self._channel.basic_qos(
                f.set_result,
                prefetch_count=prefetch_count,
                prefetch_size=prefetch_size,
                all_channels=all_channels
            )

            return await f

    @BaseChannel._ensure_channel_is_open
    async def queue_delete(self, queue_name: str, timeout: int=None,
                           if_unused: bool=False, if_empty: bool=False,
                           nowait: bool=False):

        async with self._write_lock:
            f = self._create_future(timeout=timeout)

            self._channel.queue_delete(
                callback=f.set_result,
                queue=queue_name,
                if_unused=if_unused,
                if_empty=if_empty,
                nowait=nowait
            )

            return await f

    @BaseChannel._ensure_channel_is_open
    async def exchange_delete(self, exchange_name: str, timeout: int=None,
                              if_unused=False, nowait=False):
        async with self._write_lock:
            f = self._create_future(timeout=timeout)

            self._channel.exchange_delete(
                callback=f.set_result, exchange=exchange_name,
                if_unused=if_unused, nowait=nowait
            )

            return await f

    def transaction(self) -> Transaction:
        if self._publisher_confirms:
            raise RuntimeError("Cannot create transaction when publisher "
                               "confirms are enabled")

        tx = Transaction(self._channel, self._futures.get_child())

        self.add_close_callback(tx.on_close_callback)

        tx.closing.add_done_callback(
            lambda _: self.remove_close_callback(tx.on_close_callback)
        )

        return tx


__all__ = ('Channel',)
