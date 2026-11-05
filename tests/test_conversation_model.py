import datetime
import json
import threading
import urllib.error

from gatekeeper import conversation_model as cm
from gatekeeper.conversation_model import Answers, Shadow, askable, inputs, parse, when_line

LINES = {
    "greeting_1": "Hi there! What brings you by?",
    "no_answer_1": "Don't be shy. What can I do for you?",
}
# Tuesday, March 3, 2026, 2:17 PM, local time
TS = datetime.datetime(2026, 3, 3, 14, 17).timestamp()


def answer(**changes) -> str:
    out = {
        "kind": "delivery",
        "claims": "amazon",
        "signals": [],
        "action": "reply",
        "say": "Thanks! Please leave it at the door.",
        "notify": "Amazon delivery at the door.",
    }
    return json.dumps({**out, **changes})


def visit(*turns, trigger="button", **details):
    return {
        "ts": TS + 30,
        "event_id": f"{TS:.6f}-abc",
        "trigger": trigger,
        "outcome": "completed",
        "details": details,
        "dialogue": [{"asked": asked, "heard": heard} for asked, heard in turns],
    }


def test_the_time_reads_as_it_was_trained():
    assert when_line(TS) == "It's Tuesday, March 3, 2:17 PM."
    assert when_line(datetime.datetime(2026, 3, 3, 0, 5).timestamp()) == (
        "It's Tuesday, March 3, 12:05 AM."
    )
    halloween = datetime.datetime(2026, 10, 31, 18, 40).timestamp()
    assert when_line(halloween, halloween=True) == (
        "It's Saturday, October 31, 6:40 PM (Halloween evening)."
    )


def test_each_turn_is_told_what_came_before():
    first, second = inputs(
        visit(("greeting_1", ""), ("no_answer_1", "Amazon, I've got a box")), LINES
    )
    assert first == (
        "It's Tuesday, March 3, 2:17 PM.\n"
        "Camera: a person at the door (the camera gives no other details).\n"
        "They pressed the doorbell.\n"
        "They said nothing."
    )
    assert second.endswith(
        "They pressed the doorbell.\n"
        'Earlier: (Gatekeeper) "Hi there! What brings you by?" (Visitor) "..."\n'
        'They said: "Amazon, I\'ve got a box"'
    )


def test_how_they_came_and_halloween():
    knock = inputs(visit(("greeting_1", "hello?"), trigger="knock"), LINES)[0]
    assert "\nThey knocked.\n" in knock
    seen = inputs(visit(("greeting_1", "hello?"), trigger="person"), LINES)[0]
    assert "\nThey didn't press the doorbell.\n" in seen
    spooky = inputs(visit(("greeting_1", "Trick or treat!"), occasion="halloween"), LINES, "bowl")
    assert spooky[0].startswith(
        "It's Tuesday, March 3, 2:17 PM (Halloween evening).\n"
        "House setting: treats in a bowl by the door\n"
    )
    unset = inputs(visit(("greeting_1", "Trick or treat!"), occasion="halloween"), LINES)
    assert "House setting" not in unset[0]


def test_only_the_last_three_exchanges_and_six_turns():
    turns = [("greeting_1", f"answer {n}") for n in range(8)]
    told = inputs(visit(*turns), LINES)
    assert len(told) == cm.MAX_TURNS
    assert told[5].count("(Visitor)") == 3
    assert '(Visitor) "answer 2"' in told[5] and '"answer 1"' not in told[5]


def test_a_line_no_longer_known_is_dots():
    told = inputs(visit(("greeting_9", "hi"), ("no_answer_1", "a package")), LINES)
    assert '(Gatekeeper) "..." (Visitor) "hi"' in told[1]


def test_what_is_asked_about():
    assert askable(visit(("greeting_1", "")))
    assert not askable(visit())  # not greeted
    assert not askable({**visit(("greeting_1", "")), "trigger": "package"})


def test_a_whole_answer_is_parsed_and_a_broken_one_is_not():
    assert parse("Sure! " + answer() + " ok") == json.loads(answer())
    assert parse(answer(kind="pirate")) is None
    assert parse(answer(action="open_door")) is None
    assert parse(answer(signals=["looks_shifty"])) is None
    assert parse(answer(say="  ")) is None
    assert parse('{"kind": "delivery"') is None
    assert parse("I'm not sure.") is None


def test_the_prompt_names_every_kind_signal_and_action():
    for word in (*cm.KINDS, *cm.SIGNALS, *cm.ACTIONS):
        assert word in cm.PROMPT
    assert cm.PROMPT.startswith("You are Gatekeeper")


def shadow(tmp_path, replies, sleeps=None):
    asked = []

    def ask(url, text):
        asked.append(text)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply, "gatekeeper-test.gguf"

    s = Shadow(
        Answers(tmp_path / "conversation-model.json"),
        "http://model:8080",
        lambda: LINES,
        ask=ask,
        clock=lambda: TS + 100,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )
    return s, asked


def test_each_answer_is_kept_with_the_rules_it_breaks(tmp_path):
    s, asked = shadow(
        tmp_path,
        [answer(kind="nothing", action="ask", say="Nobody's home, who is it?"), "Hmm.", answer()],
    )
    record = visit(("greeting_1", ""), ("no_answer_1", "Amazon"), ("greeting_1", "ok"))
    result = s.answer(record)
    assert len(asked) == 3
    assert result["model"] == "gatekeeper-test.gguf"
    first, second, third = result["turns"]
    assert first["kind"] == "nothing"
    assert first["broken"] == ["occupancy: nobody's home", "occupancy: 's home"]
    assert second["unusable"] == "Hmm."
    assert third["kind"] == "delivery" and third["broken"] == []
    assert s.answers.all() == {record["event_id"]: result}


def test_a_server_that_stays_down_is_an_error(tmp_path):
    sleeps = []
    down = urllib.error.URLError("connection refused")
    s, asked = shadow(tmp_path, [down, down, down], sleeps)
    result = s.answer(visit(("greeting_1", "hi")))
    assert sleeps == list(cm.RETRY_SECONDS)
    assert result == {"at": TS + 100, "error": "URLError: <urlopen error connection refused>"}
    assert s.answers.all()[f"{TS:.6f}-abc"] == result


def test_a_server_that_comes_up_is_asked_again(tmp_path):
    sleeps = []
    s, asked = shadow(tmp_path, [ConnectionRefusedError(), answer()], sleeps)
    assert s.answer(visit(("greeting_1", "hi")))["turns"][0]["kind"] == "delivery"
    assert sleeps == [cm.RETRY_SECONDS[0]]


def test_visits_are_answered_on_the_models_thread(tmp_path):
    done = threading.Event()
    s, asked = shadow(tmp_path, [answer()])
    s.answers.add = lambda eid, result, add=s.answers.add: (add(eid, result), done.set())
    s.visit_logged({**visit(), "trigger": "package"})  # not asked about
    s.visit_logged(visit(("greeting_1", "Amazon")))
    assert done.wait(5)
    assert len(asked) == 1


def test_the_camera_line_keeps_only_what_the_model_saw():
    assert cm.camera_line(
        " 1 man, middle-aged, carrying a cardboard box, wearing a dark shirt and a face mask, "
        "no uniform visible, no vehicle in view, face is visible."
    ) == (
        "Camera: 1 man, middle-aged, carrying a cardboard box, wearing a dark shirt and a "
        "face mask, face is visible."
    )
    assert (
        cm.camera_line(
            "Camera: 5 people, adults, carrying nothing visible, wearing safety vests, no visible "
            "company or vehicle, faces are visible."
        )
        == "Camera: 5 people, adults, wearing safety vests, faces are visible."
    )
    assert cm.camera_line(
        "1 person, adult, carrying a bag, not in a uniform, face is not visible."
    ) == ("Camera: 1 person, adult, carrying a bag.")
    # Only its first sentence: the rest wanders off the form.
    assert cm.camera_line("one man, in uniform. He is standing in a hallway.") == (
        "Camera: one man, in uniform."
    )
    assert cm.camera_line("nothing, no vehicle") is None
    assert cm.camera_line("") is None


def test_the_picture_is_described_after_the_turns(tmp_path):
    described = []

    def describe(url, picture):
        described.append((url, picture))
        return " one person, carrying a pizza box, no uniform visible, face is visible."

    s, asked = shadow(tmp_path, [answer()])
    s._picture = {f"{TS:.6f}-abc": b"jpeg"}.get
    s._describe = describe
    result = s.answer(visit(("greeting_1", "Pizza!")))
    assert described == [("http://model:8080", b"jpeg")]
    assert result["camera"] == "Camera: one person, carrying a pizza box, face is visible."
    # The turns are still told the camera gives no details.
    assert cm.CAMERA in asked[0]
    # No picture: nothing about the camera.
    s, _ = shadow(tmp_path, [answer()])
    s._picture = lambda event_id: None
    assert "camera" not in s.answer(visit(("greeting_1", "hi")))


def test_a_picture_it_cant_describe_keeps_the_answers(tmp_path):
    def describe(url, picture):
        raise urllib.error.HTTPError(url, 500, "image input is not supported", {}, None)

    s, _ = shadow(tmp_path, [answer()])
    s._picture = lambda event_id: b"jpeg"
    s._describe = describe
    result = s.answer(visit(("greeting_1", "hi")))
    assert result["turns"][0]["kind"] == "delivery"
    assert result["camera_error"].startswith("HTTPError: HTTP Error 500")
    # Without the vision file it isn't asked at all.
    s, _ = shadow(tmp_path, [answer()])
    assert set(s.answer(visit(("greeting_1", "hi")))) == {"at", "model", "turns"}


def test_the_picture_is_asked_about_in_the_camera_lines_form(monkeypatch):
    sent = {}

    def chat(url, body, timeout):
        sent.update(body)
        return " one person.", "test.gguf"

    monkeypatch.setattr(cm, "_chat", chat)
    assert cm.describe("http://model:8080", b"\xff\xd8jpeg") == " one person."
    image, text = sent["messages"][0]["content"]
    assert image["image_url"]["url"] == "data:image/jpeg;base64,/9hqcGVn"
    assert text["text"] == cm.DESCRIBE and "Camera: <how many people" in cm.DESCRIBE
    # The answer begun with the form keeps the trained model to it.
    assert sent["messages"][1] == {"role": "assistant", "content": "Camera:"}
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}


def test_only_the_newest_answers_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "KEEP", 2)
    answers = Answers(tmp_path / "conversation-model.json")
    for n in range(3):
        answers.add(f"e{n}", {"at": float(n), "turns": []})
    assert list(answers.all()) == ["e1", "e2"]
