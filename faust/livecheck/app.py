"""LiveCheck - Faust Application."""
import asyncio
from datetime import timedelta
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    Type,
)

from mode.utils.compat import want_bytes
from mode.utils.objects import annotations, cached_property, qualname
from mode.utils.times import Seconds

import faust
from faust.sensors.base import Sensor
from faust.types import AgentT, AppT, ChannelT, EventT, StreamT, TP, TopicT

from . import patches
from .case import Case
from .exceptions import LiveCheckError
from .locals import current_test, current_test_stack
from .models import SignalEvent, TestExecution
from .signals import BaseSignal, Signal

__all__ = ['LiveCheck']

#: alias for mypy bug
_Case = Case

patches.patch_all()  # XXX


class LiveCheckSensor(Sensor):

    def on_stream_event_in(self,
                           tp: TP,
                           offset: int,
                           stream: StreamT,
                           event: EventT) -> None:
        test = TestExecution.from_headers(event.headers)
        if test is not None:
            stream.current_test = test
            current_test_stack.push_without_automatic_cleanup(test)

    def on_stream_event_out(self,
                            tp: TP,
                            offset: int,
                            stream: StreamT,
                            event: EventT) -> None:
        has_active_test = getattr(stream, 'current_test', None)
        if has_active_test:
            stream.current_test = None
            current_test_stack.pop()


class LiveCheck(faust.App):
    """LiveCheck application."""

    Signal: ClassVar[Type[BaseSignal]]
    Signal = Signal

    Case: ClassVar[Type[_Case]]
    Case = _Case

    #: Number of concurrent actors processing signal events.
    bus_concurrency: int = 30

    #: Number of concurrent actors executing test cases.
    test_concurrency: int = 100

    test_topic_name: str
    bus_topic_name: str

    cases: Dict[str, _Case]
    bus: ChannelT

    _resolved_signals: Dict[Tuple[str, str, Any], SignalEvent]

    @classmethod
    def for_app(cls, app: AppT, *,
                prefix: str = 'livecheck-',
                web_port: int = 9999,
                bus_concurrency: int = None,
                test_concurrency: int = None,
                **kwargs: Any) -> 'LiveCheck':
        app_id, passed_kwargs = app._default_options
        livecheck_id = f'{prefix}{app_id}'
        override = {
            'web_port': web_port,
            'bus_concurrency': bus_concurrency,
            'test_concurrency': test_concurrency,
            **kwargs}
        options = {**passed_kwargs, **override}

        livecheck_app = cls(livecheck_id, **options)
        livecheck_app._contribute_to_app(app)

        return livecheck_app

    def _contribute_to_app(self, app: AppT) -> None:
        from .patches.aiohttp import LiveCheckMiddleware
        app.web.web_app.middlewares.append(LiveCheckMiddleware())
        app.sensors.add(LiveCheckSensor())
        app.livecheck = self

    def __init__(self,
                 id: str,
                 *,
                 test_topic_name: str = 'livecheck',
                 bus_topic_name: str = 'livecheck-bus',
                 bus_concurrency: int = None,
                 test_concurrency: int = None,
                 **kwargs: Any) -> None:
        super().__init__(id, **kwargs)
        self.test_topic_name = test_topic_name
        self.bus_topic_name = bus_topic_name
        self.cases = {}
        self._resolved_signals = {}

        if bus_concurrency is not None:
            self.bus_concurrency = bus_concurrency
        if test_concurrency is not None:
            self.test_concurrency = test_concurrency

        patches.patch_all()
        self._apply_monkeypatches()
        self._connect_signals()

    @property
    def current_test(self) -> Optional[TestExecution]:
        return current_test()

    @cached_property
    def _can_resolve(self) -> asyncio.Event:
        return asyncio.Event()

    def _apply_monkeypatches(self) -> None:
        patches.patch_all()

    def _connect_signals(self) -> None:
        AppT.on_produce_message.connect(self.on_produce_attach_test_headers)

    def on_produce_attach_test_headers(
            self,
            sender: AppT,
            key: bytes = None,
            value: bytes = None,
            partition: int = None,
            timestamp: float = None,
            headers: List[Tuple[str, bytes]] = None,
            **kwargs: Any) -> None:
        test = current_test()
        if test is not None:
            if headers is None:
                raise TypeError('Produce request missing headers list')
            headers.extend([
                (k, want_bytes(v)) for k, v in test.as_headers().items()
            ])

    def case(self, *,
             name: str = None,
             probability: float = None,
             warn_empty_after: Seconds = timedelta(minutes=30),
             active: bool = None,
             test_expires: Seconds = None,
             frequency: Seconds = None,
             max_history: int = None,
             url_timeout_total: float = None,
             url_timeout_connect: float = None,
             url_error_retries: float = None,
             url_error_delay_min: float = None,
             url_error_delay_backoff: float = None,
             url_error_delay_max: float = None,
             base: Type[_Case] = Case) -> Callable[[Type], _Case]:
        base_case = base

        def _inner(cls: Type) -> _Case:
            case_cls = type(cls.__name__, (cls, base_case), {
                '__module__': cls.__module__,
                'app': self,
            })
            fields, defaults = annotations(
                case_cls,
                stop=base_case,
                skip_classvar=True,
                localns={case_cls.__name__: case_cls},
            )
            signals = []

            def find_signals() -> Iterable[str]:
                for attr_name, attr_type in fields.items():
                    actual_type = getattr(attr_type, '__origin__', attr_type)
                    if issubclass(actual_type, BaseSignal):
                        yield attr_name

            for i, attr_name in enumerate(find_signals()):
                signal = getattr(case_cls, attr_name, None)
                if signal is None:
                    signal = self.Signal(name=attr_name, index=i + 1)
                    setattr(case_cls, attr_name, signal)
                    signals.append(signal)
                else:
                    signal.index = i + 1

            return self.add_case(case_cls(
                app=self,
                name=self._prepare_case_name(name or qualname(cls)),
                active=active,
                probability=probability,
                warn_empty_after=warn_empty_after,
                signals=signals,
                test_expires=test_expires,
                frequency=frequency,
                url_timeout_total=url_timeout_total,
                url_timeout_connect=url_timeout_connect,
                url_error_retries=url_error_retries,
                url_error_delay_min=url_error_delay_min,
                url_error_delay_backoff=url_error_delay_backoff,
                url_error_delay_max=url_error_delay_max,
            ))
        return _inner

    def add_case(self, case: _Case) -> _Case:
        self.cases[case.name] = case
        return case

    async def on_start(self) -> None:
        await super().on_start()
        self._install_bus_agent()
        self._install_test_execution_agent()

    async def on_started(self) -> None:
        await super().on_started()
        for case in self.cases.values():
            await self.add_runtime_dependency(case)

    def _install_bus_agent(self) -> AgentT:
        return self.agent(
            channel=self.bus,
            concurrency=self.bus_concurrency,
        )(self._populate_signals)

    def _install_test_execution_agent(self) -> AgentT:
        return self.agent(
            channel=self.pending_tests,
            concurrency=self.test_concurrency,
        )(self._execute_tests)

    async def _populate_signals(self, events: StreamT[SignalEvent]) -> None:
        async for test_id, event in events.items():
            event.case_name = self._prepare_case_name(event.case_name)
            case = self.cases[event.case_name]
            await case.resolve_signal(test_id, event)

    async def _execute_tests(self, tests: StreamT[TestExecution]) -> None:
        async for test_id, test in tests.items():
            test.case_name = self._prepare_case_name(test.case_name)
            case = self.cases[test.case_name]
            try:
                await case.execute(test)
            except LiveCheckError:
                pass

    def _prepare_case_name(self, name: str) -> str:
        if name.startswith('__main__.'):
            if not self.conf.origin:
                raise RuntimeError('LiveCheck app missing origin argument')
            return self.conf.origin + name[8:]
        return name

    @cached_property
    def bus(self) -> TopicT:
        return self.topic(
            self.bus_topic_name,
            key_type=str,
            value_type=SignalEvent,
        )

    @cached_property
    def pending_tests(self) -> TopicT:
        return self.topic(
            self.test_topic_name,
            key_type=str,
            value_type=TestExecution,
        )
