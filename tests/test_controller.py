"""Exercise timing, replacement, failures, limits, stop, and shutdown."""

import asyncio
from unittest.mock import AsyncMock, call

import pytest
from homeassistant.helpers.storage import Store

from custom_components.powerwall_force import controller as module
from custom_components.powerwall_force.controller import DispatchController
from custom_components.powerwall_force.protocol import CommandRejected, PowerwallError


@pytest.fixture
async def control(hass):
    client = AsyncMock()
    instance = DispatchController(
        client,
        5000,
        10000,
        lambda coro, name: asyncio.create_task(coro, name=name),
        lambda: None,
        Store(hass, 1, "dispatch_test", atomic_writes=True),
    )
    yield instance
    await instance.async_stop(close=True)


async def test_charge_maximum_and_replace_with_discharge(control):
    await control.async_start("charge", 120, None)
    control.client.async_dispatch.assert_awaited_with(-5000, 30)
    old_task = control._task
    await control.async_start("discharge", 90, 1200)
    await asyncio.sleep(0)
    assert old_task.done()
    control.client.async_dispatch.assert_awaited_with(1200, 30)
    assert control.mode == "discharge"
    assert control.power_w == 1200
    control.client.async_resume.assert_not_awaited()


async def test_duration_expires_and_releases(control, monkeypatch):
    monkeypatch.setattr(module, "RENEW_SECONDS", 0.02)
    await control.async_start("charge", 1, 1000)
    await asyncio.wait_for(control._task, timeout=2)
    assert control.mode == "idle"
    assert control.client.async_dispatch.await_count > 2
    assert all(c.args[1] == 1 for c in control.client.async_dispatch.await_args_list)
    assert all(
        0 < c.kwargs["time_budget"] <= 1 for c in control.client.async_dispatch.await_args_list[1:]
    )
    control.client.async_resume.assert_awaited_once()


async def test_stop_cancels_renewal(control, monkeypatch):
    monkeypatch.setattr(module, "RENEW_SECONDS", 0.01)
    await control.async_start("charge", 60, 1200)
    await control.async_stop()
    await asyncio.sleep(0.03)
    control.client.async_dispatch.assert_awaited_once()
    control.client.async_resume.assert_awaited_once()
    assert control.mode == "idle"


async def test_failed_renewal_does_not_retry(control, monkeypatch):
    monkeypatch.setattr(module, "RENEW_SECONDS", 0.01)
    control.client.async_dispatch.side_effect = [None, PowerwallError("Network lost")]
    await control.async_start("discharge", 300, 1000)
    await asyncio.wait_for(control._task, timeout=1)
    assert control.mode == "error"
    assert control.last_error == "Network lost"
    assert control.restore_status == "confirmed"
    assert control.restore_error is None
    assert control.client.async_dispatch.await_count == 2
    assert control.client.async_dispatch.await_args.kwargs["time_budget"] == module.RENEW_TIMEOUT
    control.client.async_resume.assert_awaited_once()


async def test_failed_renewal_retries_only_resume_and_preserves_failure(control, monkeypatch):
    monkeypatch.setattr(module, "RENEW_SECONDS", 0.01)
    monkeypatch.setattr(module, "RESTORE_INTERVAL", 0.05)
    control.client.async_dispatch.side_effect = [None, PowerwallError("Renewal timed out")]
    control.client.async_resume.side_effect = [PowerwallError("Offline"), None]
    await control.async_start("charge", 300, 1000)
    await asyncio.wait_for(control._task, 1)
    assert control.restore_status == "pending"
    assert control.restore_error == "Offline"
    assert await control._store.async_load() is True
    with pytest.raises(PowerwallError, match="resume is still pending"):
        await control.async_start("discharge", 300, 2000)
    await asyncio.wait_for(control._recovery_task, 1)
    assert control.restore_status == "confirmed"
    assert control.restore_error is None
    assert control.mode == "error"
    assert control.last_error == "Renewal timed out"
    assert control.client.async_dispatch.await_count == 2
    assert control.client.async_resume.await_count == 2
    assert await control._store.async_load() is False


async def test_manual_stop_retries_resume_and_clears_status(control, monkeypatch):
    monkeypatch.setattr(module, "RESTORE_INTERVAL", 0.01)
    control.client.async_resume.side_effect = PowerwallError("Offline")
    await control.async_start("charge", 300, 1000)
    with pytest.raises(PowerwallError, match="Offline"):
        await control.async_stop()
    await asyncio.wait_for(control._recovery_task, 1)
    assert control.client.async_resume.await_count == module.RESTORE_ATTEMPTS
    assert control.restore_status == "pending"
    assert await control._store.async_load() is True

    control.client.async_resume.side_effect = None
    await control.async_stop()
    await control.async_start("charge", 300, 1000)
    assert control.mode == "charge"
    assert control.restore_status is None
    assert control.client.async_resume.await_count == module.RESTORE_ATTEMPTS + 1


async def test_missing_recovery_record_prevents_new_dispatch(control, monkeypatch):
    monkeypatch.setattr(control._store, "async_save", AsyncMock())
    with pytest.raises(PowerwallError, match="Could not save the dispatch recovery record"):
        await control.async_start("charge", 300, 1000)
    control.client.async_dispatch.assert_not_awaited()
    control.client.async_resume.assert_not_awaited()


async def test_cancelled_stop_cannot_clear_the_next_dispatch_record(control, monkeypatch):
    clearing = asyncio.Event()
    finish_write = asyncio.Event()
    write_data = control._store._async_write_data

    async def slow_clear(path, data):
        if data["data"] is False:
            clearing.set()
            await finish_write.wait()
        await write_data(path, data)

    monkeypatch.setattr(control._store, "_async_write_data", slow_clear)
    await control.async_start("charge", 300, 1000)
    stop = asyncio.create_task(control.async_stop())
    await asyncio.wait_for(clearing.wait(), 1)
    stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop

    start = asyncio.create_task(control.async_start("discharge", 300, 2000))
    await asyncio.sleep(0)
    assert control.client.async_dispatch.await_count == 1
    finish_write.set()
    await asyncio.wait_for(start, 1)
    assert await control._store.async_load() is True
    assert control.mode == "discharge"


async def test_late_ack_does_not_allow_renewing_past_possible_lease_expiry(control, monkeypatch):
    monkeypatch.setattr(module, "LEASE_SECONDS", 1)
    monkeypatch.setattr(module, "RENEW_SECONDS", 0.01)

    async def delayed_ack(watts, seconds):
        await asyncio.sleep(1.02)

    control.client.async_dispatch.side_effect = delayed_ack
    await control.async_start("charge", 60, 1000)
    await asyncio.wait_for(control._task, timeout=1)
    control.client.async_dispatch.assert_awaited_once_with(-1000, 1)
    assert control.mode == "error"
    assert "lease may have expired" in control.last_error
    control.client.async_resume.assert_awaited_once()


async def test_failed_start_releases_uncertain_dispatch(control):
    control.client.async_dispatch.side_effect = PowerwallError("No acknowledgement")
    with pytest.raises(PowerwallError):
        await control.async_start("charge", 60, 1000)
    control.client.async_resume.assert_awaited_once()
    assert control._task is None
    assert control.mode == "error"


@pytest.mark.parametrize(
    "duration,power",
    [
        (0, 1000),
        (86401, 1000),
        (float("nan"), 1000),
        (10, 0),
        (10, -1),
        (10, 5001),
        (10, float("inf")),
        (10, float("nan")),
    ],
)
async def test_invalid_requests_do_not_touch_hardware(control, duration, power):
    with pytest.raises(ValueError):
        await control.async_start("charge", duration, power)
    control.client.async_dispatch.assert_not_awaited()
    control.client.async_resume.assert_not_awaited()


async def test_failed_stop_still_cancels_renewals(control):
    await control.async_start("charge", 60, 1000)
    control.client.async_resume.side_effect = PowerwallError("Offline")
    with pytest.raises(PowerwallError):
        await control.async_stop()
    assert control._task is None
    assert control.mode == "error"


async def test_shutdown_blocks_new_dispatch(control):
    await control.async_start("charge", 60, 1000)
    await control.async_stop(close=True)
    with pytest.raises(PowerwallError, match="unloading"):
        await control.async_start("discharge", 60, 1000)


async def test_rejected_configured_discharge_maximum_is_explained(control):
    control.max_discharge = 11000
    control.client.async_dispatch.side_effect = CommandRejected(11)
    with pytest.raises(PowerwallError, match="11000 W.*configured maximum") as rejection:
        await control.async_start("discharge", 60, None)
    assert "power_w was omitted" in str(rejection.value)
    assert "Reconfigure" in str(rejection.value)
    assert control.last_error == str(rejection.value)
    control.client.async_dispatch.assert_awaited_once_with(11000, 30)
    assert control._task is None
    # An accepted explicit value remains usable below the configured maximum.
    control.client.async_dispatch.side_effect = None
    await control.async_start("discharge", 60, 9000)
    control.client.async_dispatch.assert_awaited_with(9000, 30)
    assert control.mode == "discharge"


@pytest.mark.parametrize(
    ("mode", "watts", "limit"), [("charge", -5000, -4000), ("discharge", 10000, 9000)]
)
async def test_reported_limit_retries_once_after_confirmed_resume(control, mode, watts, limit):
    failure = CommandRejected(11, f"power {watts} exceeds {mode} limit {limit}")
    control.client.async_dispatch.side_effect = [failure, None, None]
    await control.async_start(mode, 60, abs(watts))
    assert control.client.mock_calls[:2] == [call.async_dispatch(watts, 30), call.async_resume()]
    assert control.client.async_dispatch.await_args.args == (limit, 30)
    assert control.power_w == watts
    assert control.dispatch_power_w == limit
    assert control.restore_status is None
    await control.async_start(mode, 60, abs(watts))
    control.client.async_dispatch.assert_awaited_with(limit, 30)


async def test_limit_rejection_without_resume_ack_does_not_retry_power(control, monkeypatch):
    monkeypatch.setattr(module, "RESTORE_INTERVAL", 0.01)
    control.client.async_dispatch.side_effect = CommandRejected(
        11, "power 10000 exceeds discharge limit 9000"
    )
    control.client.async_resume.side_effect = [PowerwallError("Offline"), None]
    with pytest.raises(PowerwallError, match="status 11"):
        await control.async_start("discharge", 60, 10000)
    await asyncio.wait_for(control._recovery_task, 1)
    control.client.async_dispatch.assert_awaited_once_with(10000, 30)
    assert control.restore_status == "confirmed"
    assert control.mode == "error"
