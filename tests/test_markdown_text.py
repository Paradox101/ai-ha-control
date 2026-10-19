from app.text import plain_text, strip_emphasis


def test_plain_text_strips_markdown():
    md = "## Thuis\n**Personen**\n- Vincent is *thuis*\n  - sinds `10:00`\n[link](http://x)"
    assert plain_text(md) == "Thuis\nPersonen\n• Vincent is thuis\n  • sinds 10:00\nlink"


def test_plain_text_keeps_ordinary_text():
    assert plain_text("2 * 3 = 6 en sensor_x_y") == "2 * 3 = 6 en sensor_x_y"


def test_plain_text_for_speech_reads_lists_as_sentences():
    assert plain_text("**Personen**\n- Vincent thuis\n- Anna weg", speech=True) == \
        "Personen. Vincent thuis. Anna weg."


def test_strip_emphasis():
    assert strip_emphasis("**Personen**: `x`") == "Personen: x"


async def test_notifications_and_announcements_are_plain_text():
    from unittest.mock import AsyncMock, MagicMock

    from app.notify import Notifier

    config = MagicMock(quiet_hours="", digest_time="", escalate_minutes=0, person_notify=[], notify_service="notify.x",
                       announce_players=["media_player.woonkamer"], tts_entity="tts.piper")
    ha, db = MagicMock(call_service=AsyncMock()), MagicMock()
    db.add_event.return_value = 1
    await Notifier(config, ha, db).send("**Let op**", "- Raam *open*", announce="**Personen**\n- Vincent thuis")
    assert db.add_event.call_args.args[1:3] == ("Let op", "• Raam open")
    push, speak = ha.call_service.call_args_list
    assert push.args[2]["title"] == "Let op" and push.args[2]["message"] == "• Raam open"
    assert speak.args[2]["message"] == "Personen. Vincent thuis."
