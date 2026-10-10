ry") if same_consultation else None
                    ),
                )
                if topic == "consultation":
                    consultation = payload.get("consultation") or {}
                    answered = set(
                        prior_consultation.get("answered_fields") or []
                        if same_consultation
                        else []
                    )
                    fields = (
                        ("main_concern", "skin_feel", "skin_flags")
                        if consultation_kind == "facial"
                        else ("massage_reason", "massage_areas", "pressure_preference")
                    )
                    answered.update(
                        field
                        for field in fields
                        if str(args.get(field) or "").strip()
                    )
                    if consultation_kind == "massage" and args.get("safety_answered") is True:
                        answered.add("safety_answered")
                    required_fields = (
                        ["main_concern", "skin_feel", "skin_flags"]
                        if consultation_kind == "facial"
                        else [
                            "massage_reason",
                            "massage_areas",
                            "pressure_preference",
                            "safety_answered",
                        ]
                    )
                    consultation["answered_fields"] = sorted(answered)
                    consultation["remaining_questions"] = [
                        field for field in required_fields if field not in answered
                    ]
                    addon_names = {
                        str(item.get("service", {}).get("name") or "").strip()
                        for item in consultation.get("addons") or []
                        if isinstance(item, dict)
                    }
                    addon_names.discard("")
                    if addon_names:
                        previously_presented = {
                            str(name).strip()
                            for name in self.session.entities.get(
                                "consultation_addon_names", []
                            )
                            if str(name).strip()
                        }
                        self.session.entities["consultation_addon_names"] = sorted(
                            previously_presented | addon_names
                        )
                    selected = consultation.get("selected_service") or {}
                    recommended = consultation.get("service") or {}
                    self.session.entities["consultation_state"] = {
                        "kind": consultation_kind,
                        "answered_fields": sorted(answered),
                        "category": consultation.get("category"),
                        "recommended_service": recommended.get("name"),
                        "selected_service": selected.get("name"),
                        "selected_duration_minutes": args.get(
                            "selected_duration_minutes"
                        ),
                        "duration_choices": [
                            item.get("minutes")
                            for item in (consultation.get("durations") or {}).get(
                                "choices", []
                            )
                            if item.get("minutes") in {30, 60}
                        ],
                    }
                    await self._persist_session()
        except Exception:
            logger.exception("call %s: spa fact lookup failed", self.call_id)
            return json.dumps({
                "status": "unknown",
                "message": "I don't have that information available.",
            })
        return json.dumps(payload)

    async def _offer_staff_callback(self) -> None:
        if self.session.entities.get("callback_offer_pending"):
            return
        self.session.entities["callback_offer_pending"] = True
        self.session.entities.pop("awaiting_caller_name", None)
        self._response_needed_after_tool = False
        self._restricted_response_needed_after_tool = False
        self._pending_forced_tool_message = None
        await self._cancel_active_response()
        stop = getattr(self, "stop_hold_tone", None)
        if stop:
            await stop()
        await self._send_force_message(
            "I'm having trouble completing this appointment. Would it be okay if I asked a service provider to call you back to help at their earliest availability?"
        )

    async def _run_request_callback(self, raw_arguments: str) -> str:
        if not self.session.entities.get("callback_authorized"):
            return json.dumps({"status": "consent_required", "message": "May I ask a service provider to call you back?"})
        if self.session.entities.get("callback_saved"):
            return json.dumps({"status": "stored", "kind": "callback"})
        from app.models.follow_up_request import FollowUpRequest
        from app.services.secure_payment import contains_sensitive_payment
        from app.services.staff_notifications import callback_request_payload, notify_staff

        try:
            args = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        if contains_sensitive_payment(args):
            return json.dumps({
                "status": "rejected",
                "message": "Please don't share card details for this callback request.",
            })
        try:
            async with AsyncSessionLocal() as db:
                routing = await _prepare(db, self.session)
                spa = routing.spa
                payload = callback_request_payload(
                    spa_id=getattr(spa, "id", None),
                    call_sid=self.call_id,
                    caller_phone=self.session.customer_phone,
                    caller_name=args.get("caller_name") or get_draft(self.session).caller_name,
                    reason=args.get("reason"),
                    preferred_window=args.get("preferred_window"),
                )
                if spa is None:
                    return json.dumps({"status": "error", "message": "The business could not be identified."})
                if spa is not None:
                    db.add(FollowUpRequest(
                        spa_id=spa.id,
                        call_sid=payload["call_sid"],
                        caller_phone=payload["caller_phone"],
                        caller_name=payload["caller_name"],
                        reason=payload["reason"],
                        preferred_window=payload["preferred_window"],
                        kind="callback",
                        status="open",
                    ))
                    await db.commit()
                    self.session.entities["callback_saved"] = True
                    await notify_staff(
                        spa,
                        "callback_requested",
                        f"Callback requested on call {self.call_id}.",
                    )
        except Exception:
            logger.exception("call %s: callback request failed", self.call_id)
            if self.session.entities.get("callback_saved"):
                return json.dumps({"status": "stored", "kind": "callback", "notification_status": "failed"})
            return json.dumps({
                "status": "error",
                "message": "I could not save that callback request.",
            })
        return json.dumps({
            "status": "stored",
            "kind": "callback",
            "message": "I've saved the callback request for the team.",
        })

    # Tools that reach the live scheduling provider by probing one specific
    # instant. Nothing stops the model from calling one of these repeatedly
    # with a different self-guessed time to simulate a search — this is
    # exactly what happened on the live call that motivated this guard: eight
    # straight `check_availability` calls with 30-minute-apart fabricated
    # times, all inside one response. Capped per-response below.
    # MANAGE_APPOINTMENT_TOOL is included here even though it is not currently
    # offered in VOICE_TOOLS (see the module docstring's tool table). It is a
    # legacy single-step tool whose handler still exists and writes directly
    # via attempt_booking with a caller-supplied requested_start_iso. If it is
    # ever re-added to VOICE_TOOLS, this ensures the temporal-grounding guard
    # covers it too, rather than silently bypassing the one tool that can
    # write a booking from a self-invented time.
    _AVAILABILITY_PROBE_TOOLS = frozenset(
        {
            CHECK_AVAILABILITY_TOOL["name"],
            PROPOSE_APPOINTMENT_TOOL["name"],
            MANAGE_APPOINTMENT_TOOL["name"],
        }
    )

    def _last_user_utterance(self) -> str | None:
        for turn in reversed(self.session.history):
            if turn.get("role") == "user":
                return turn.get("content")
        return None

    def _recent_user_utterances(self, limit: int) -> list[str]:
        found: list[str] = []
        for turn in reversed(self.session.history):
            if turn.get("role") != "user":
                continue
            content = turn.get("content")
            if content:
                found.append(content)
            if len(found) >= limit:
                break
        return found

    def _active_established_date(self) -> date | None:
        """The calendar date the caller most recently, unambiguously
        established — for a LATER turn that names only a time to complete.

        Walks backward through the caller's prior turns (not the current
        one) and stops at the first of:
          * an explicit cancellation with no date in the same breath
            ("never mind that", "forget it") -> no active date;
          * a turn naming a date -> that date, best-effort parsed. Stopping
            at the first (most recent) hit is what lets a correction
            ("actually, make that October 2nd") supersede an earlier date
            instead of both being "recent" and ambiguous.
        Returns None rather than guessing if the date can't be confidently
        parsed, or if nothing recent enough establishes one at all — see
        `_RECENT_DATE_CONTEXT_TURNS`.
        """
        today = self._now().date()
        recent = self._recent_user_utterances(_RECENT_DATE_CONTEXT_TURNS + 1)
        for utterance in recent[1:]:  # [0] is the current turn itself
            has_date = _DATE_MENTION_RE.search(utterance)
            if _DATE_CANCEL_RE.search(utterance) and not has_date:
                return None
            if has_date:
                # The most recent date-naming turn wins outright, even if it
                # can't be confidently resolved to a value — falling through
                # to an OLDER turn here would let a vague correction
                # ("actually, a different day") un-cancel a stale date.
                return _extract_explicit_date(utterance, today)
        return None

    def _spoken_date(self, utterance: str | None, today: date) -> tuple[date | None, str]:
        """Date the caller named, and whether it came from a weekday."""
        text = utterance or ""
        explicit = _extract_explicit_date(text, today)
        if explicit is not None and _extract_weekday_date(text, today) != explicit:
            return explicit, "explicit"
        if _extract_weekday_date(text, today) is not None:
            return _extract_weekday_date(text, today), "weekday"
        if explicit is not None:
            return explicit, "explicit"
        active = self._active_established_date()
        return active, "active" if active is not None else "none"

    def _prior_date_is_weekday(self) -> bool:
        """True when the date still in play came from a weekday name, not a calendar date."""
        today = self._now().date()
        recent = self._recent_user_utterances(_RECENT_DATE_CONTEXT_TURNS + 1)
        for utterance in recent[1:]:
            has_date = bool(_DATE_MENTION_RE.search(utterance))
            if _DATE_CANCEL_RE.search(utterance) and not has_date:
                return False
            if has_date:
                weekday = _extract_weekday_date(utterance, today)
                resolved = _extract_explicit_date(utterance, today)
                return weekday is not None and resolved == weekday
        return False

    def _spoken_exact_start(self) -> datetime | None:
        """Local datetime for a weekday or date plus a clock time the caller said.

        The model's own ISO is not used. "Thursday at 4pm" becomes the next
        Thursday at 16:00 in the spa timezone. If that moment has already
        passed and the caller named a weekday rather than a calendar date,
        it rolls forward one week.
        """
        utterance = self._last_user_utterance()
        if not utterance:
            return None
        clock = _extract_clock_time(utterance)
        if clock is None:
            return None
        now = self._now()
        spoken_date, source = self._spoken_date(utterance, now.date())
        # Calendar dates and "tomorrow" stay exactly as the model sent them.
        # Only a weekday ("Thursday at 4pm", or "4pm" after "Thursday") is
        # resolved here, so a wrong model date cannot reach the provider.
        if spoken_date is None or source in {"none", "explicit"}:
            return None
        if source == "active" and not self._prior_date_is_weekday():
            return None
        start = datetime.combine(spoken_date, clock, tzinfo=self._tz)
        if start <= now:
            start = start + timedelta(days=7)
        return start

    def _spoken_day_part_window(self) -> tuple[datetime, datetime] | None:
        """Local window for "Saturday afternoon" when no clock time was said."""
        utterance = self._last_user_utterance()
        if not utterance or _extract_clock_time(utterance):
            self.session.entities.pop("requested_availability_window", None)
            return None
        hours = _extract_day_part(utterance)
        date_wide = bool(re.search(
            r"\b(availability|openings|available|what\s+times)\b", utterance, re.I
        )) or bool(self.session.entities.get("requested_availability_window") and _DATE_MENTION_RE.search(utterance))
        if hours is None and not date_wide:
            pending = self.session.entities.get("requested_availability_window")
            if pending and not _DATE_MENTION_RE.search(utterance):
                start, end = (_parse_dt(value, self._tz) for value in pending)
                now = self._now()
                if start is not None and end is not None and end > now:
                    return max(start, now), end
            return None
        now = self._now()
        spoken_date, source = self._spoken_date(utterance, now.date())
        if spoken_date is None or source == "none":
            return None
        if hours is None:
            start = datetime.combine(spoken_date, dt_time.min, tzinfo=self._tz)
            end = datetime.combine(spoken_date + timedelta(days=1), dt_time.min, tzinfo=self._tz)
            start = max(start, now)
        else:
            start = datetime.combine(spoken_date, hours[0], tzinfo=self._tz)
            end = datetime.combine(spoken_date, hours[1], tzinfo=self._tz)
        weekday_based = source == "weekday" or (
            source == "active" and self._prior_date_is_weekday()
        )
        if weekday_based and end <= now:
            start = start + timedelta(days=7)
            end = end + timedelta(days=7)
        return start, end

    def _caller_requested_earliest(self) -> bool:
        """Tool args are not proof that the caller requested an earliest search."""
        utterance = self._last_user_utterance() or ""
        return bool(_EARLIEST_REQUEST_RE.search(utterance))

    def _time_key(self, requested_start_iso: str | None) -> str | None:
        """Canonical UTC key for comparing caller/provider exact timestamps."""
        if not requested_start_iso:
            return None
        target = _parse_dt(requested_start_iso, self._tz)
        if target is None:
            return None
        return target.astimezone(timezone.utc).isoformat()

    def _cancelled_slot_reference(self) -> dict[str, Any] | None:
        utterance = self._last_user_utterance() or ""
        if (_DATE_MENTION_RE.search(utterance) or _extract_clock_time(utterance)
                or not re.search(r"\b(?:same|that)\s+(?:time|slot|appointment)\b", utterance, re.I)):
            return None
        return self.session.entities.get("last_cancelled_appointment")

    def _is_time_grounded(self, requested_start_iso: str | None) -> bool:
        """Whether an exact-time probe has real evidence behind it.

        Allowed sources:
        1. the caller's current fresh turn explicitly mentions a time of day;
        2. the exact timestamp already passed grounding earlier for that same
           caller-stated time and is merely being retried;
        3. the exact timestamp matches a provider-confirmed offered/pinned slot.

        Crucially, (2) is exact-instant only. A retry of 4:00 PM may reuse the
        prior grounding; 4:30 PM or another date does not inherit permission.
        When the caller states a new time, `_flush_caller_turn` clears the old
        durable timestamp(s).
        """
        key = self._time_key(requested_start_iso)
        if key is None:
            return False
        cancelled = self._cancelled_slot_reference()
        if cancelled and key == self._time_key(cancelled.get("start_iso")):
            # Caller authorization to SEARCH only. A cancelled appointment is
            # never evidence that its slot is currently free or booked.
            return True

        # Same exact caller-grounded instant may be retried without forcing the
        # caller to repeat themselves.
        if key in self._grounded_exact_times:
            logger.info(
                "call %s: reusing previously grounded exact time %s",
                self.call_id,
                requested_start_iso,
            )
            return True

        target = _parse_dt(requested_start_iso, self._tz)
        if target is None:
            return False

        # Provider-returned slots remain authoritative independent of caller
        # transcript wording.
        draft = get_draft(self.session)
        candidates = list(draft.alternative_slots or [])
        candidates.extend((draft.verified_availability or {}).get("slots", []))
        if draft.selected_slot:
            candidates.append(draft.selected_slot)
        for slot in candidates:
            slot_dt = _parse_provider_iso(slot.get("start"))
            if (
                slot_dt is not None
                and slot_dt.astimezone(timezone.utc)
                == target.astimezone(timezone.utc)
            ):
                return True

        # A fresh caller turn that actually contains a time can ground one exact
        # timestamp — but only combined with a date the caller actually named,
        # in this turn or as the most recent unretracted date established
        # earlier. A bare "3pm" with no date anywhere recent, or a date that
        # doesn't match what's actually active (superseded or cancelled),
        # must not be treated as grounding a timestamp on some date the model
        # invented or resurrected. `_handle_function_call` records the
        # accepted timestamp immediately after this check, so later retries
        # must match it exactly.
        if self._user_turn_count > self._last_probe_turn_count:
            last_utterance = self._last_user_utterance()
            if last_utterance and _TIME_MENTION_RE.search(last_utterance):
                spoken = self._spoken_exact_start()
                if spoken is not None:
                    return (
                        target.astimezone(timezone.utc)
                        == spoken.astimezone(timezone.utc)
                    )
                if _DATE_MENTION_RE.search(last_utterance):
                    return True
                active_date = self._active_established_date()
                if active_date is not None and active_date == target.date():
                    return True

        return False

    def _remember_grounded_exact_time(self, requested_start_iso: str | None) -> None:
        """Remember only the exact timestamp that just passed the grounding gate."""
        key = self._time_key(requested_start_iso)
        if key is None:
            return
        if key not in self._grounded_exact_times:
            logger.info(
                "call %s: remembering grounded exact time %s",
                self.call_id,
                requested_start_iso,
            )
        self._grounded_exact_times.add(key)

    async def _send_force_message(self, message: str, *, protect_playback: bool = False) -> None:
        """Speak an authoritative backend result verbatim without another model turn.

        xAI's `force_message` synthesizes the supplied text directly and creates
        its own normal response lifecycle. Do not follow it with response.create.
        This is ideal for final booking confirmations because the calendar result,
        not the language model, is the source of truth.
        """
        if message == CARD_ON_FILE_POLICY:
            draft = get_draft(self.session)
            if not (draft.confirmation_authorized and
                    self.session.entities.get("caller_confirmed_revision") == draft.draft_revision):
                return
            self._card_policy_playback_revision = draft.draft_revision
            self._card_policy_consent_turn = self._user_turn_count
            self._card_policy_response_id = None
        elif message == CARD_LINK_PERMISSION_QUESTION:
            draft = get_draft(self.session)
            self.session.entities["card_link_consent_pending_revision"] = draft.draft_revision
        if protect_playback:
            self._protect_playback = True
        if "anything else I can help you with today" in message:
            self._awaiting_wrap_up = True
        if (
            self._should_cancel_restart_greeting()
            and self._looks_like_session_restart_greeting(message)
        ):
            truth(
                "CALL_GREETING_SUPPRESSED",
                call_sid=self.call_id,
                reason="already_greeted",
            )
            return
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "force_message",
                    "role": "assistant",
                    "interruptible": True,
                    "content": [{"type": "output_text", "text": message}],
                },
            }
        )

    def _authoritative_tool_followup(self, tool_name: str, output: str) -> str | None:
        """Return exact caller-facing speech for terminal authoritative outcomes."""
        try:
            payload = json.loads(output)
        except (TypeError, json.JSONDecodeError):
            return None

        status = str(payload.get("status") or "").lower()

        if tool_name == CANCEL_APPOINTMENT_TOOL["name"]:
            if status != "cancelled" or not payload.get("appointment_id"):
                return None
            service = str(payload.get("service") or "appointment").strip()
            cancelled_start = payload.get("cancelled_start_iso")
            if not cancelled_start:
                return (
                    f"Your {service} appointment has been cancelled. "
                    "Would you like to book another time?"
                )
            try:
                dt = datetime.fromisoformat(str(cancelled_start).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                local = dt.astimezone(self._tz)
                day = f"{local.strftime('%A, %B')} {local.day}"
                spoken_time = local.strftime("%I:%M %p").lstrip("0")
                if spoken_time.endswith(":00 AM") or spoken_time.endswith(":00 PM"):
                    spoken_time = spoken_time.replace(":00 ", " ")
                return (
                    f"Your {service} appointment for {day} at {spoken_time} has been cancelled. "
                    "Would you like to book another time?"
                )
            except (TypeError, ValueError):
                return (
                    f"Your {service} appointment has been cancelled. "
                    "Would you like to book another time?"
                )

        if tool_name != CONFIRM_APPOINTMENT_TOOL["name"]:
            return None
        if status == "conflict":
            alternatives = grounded_availability_speech(self.session, self._tz)
            return (
                "It looks like that time was just taken, but I can check the next closest openings for you. "
                + (alternatives + " Which time would you prefer?" if alternatives
                   else "Would you like me to check another day?")
            )
        if (
            status == BookingOutcome.ERROR.value
            and self.session.entities.get("booking_reconciliation_required")
        ):
            # The create reached the provider path but no authoritative active
            # booking made it back to the conversation. Ask once whether an SMS
            # arrived, then reconcile read-only on the caller's answer.
            return self._arm_confirmation_text_check()
        if status not in {"booked", "rescheduled"}:
            return None
        if not payload.get("appointment_id") or not payload.get("external_booking_id"):
            return None

        self._booking_completed_turn = self._user_turn_count
        self.session.entities.pop("caller_grounded_service_durations", None)

        confirmation_key = str(payload["external_booking_id"]) + ":" + str(self.session.confirmed_datetime)
        if self.session.entities.get("spoken_booking_confirmation") == confirmation_key:
            return ""
        self.session.entities["spoken_booking_confirmation"] = confirmation_key
        service = (self.session.selected_service or "appointment").strip()
        provider = (get_draft(self.session).preferred_staff or "").strip()
        with_provider = f" with {provider}" if provider else ""
        confirmed = self.session.confirmed_datetime

        def finish(sentence: str) -> str:
            slot = get_draft(self.session).selected_slot or {}
            if slot.get("visit_segments"):
                itinerary = []
                for item in slot["visit_segments"]:
                    begins = datetime.fromisoformat(item["start"]).astimezone(self._tz).strftime("%I:%M %p").lstrip("0")
                    ends = datetime.fromisoformat(item["end"]).astimezone(self._tz).strftime("%I:%M %p").lstrip("0")
                    staff = f" with {item['provider_name']}" if item.get("provider_name") else ""
                    itinerary.append(f"{item['service_name']} from {begins} to {ends}{staff}")
                sentence = sentence.replace("Is there anything else I can help you with today?", "")
                sentence += " Your visit includes " + "; then ".join(itinerary) + f". Total visit time is {slot['duration_minutes']} minutes. Is there anything else I can help you with today?"
            if slot.get("team_member_id") or slot.get("visit_segments"):
                check_in = (
                    f" {provider} will check in with you when you arrive."
                    if provider
                    else " Your service provider will check in with you when you arrive."
                )
                question = " Is there anything else I can help you with today?"
                if sentence.endswith(question):
                    sentence = sentence[: -len(question)] + check_in + question
                else:
                    sentence += check_in
            if payload.get("card_status") == "pending_card":
                sentence = sentence.replace("You're all set. ", "")
                sentence = sentence.replace("is confirmed", "is reserved pending your card on file")
            clause = booking_card_speech(payload.get("card_status"), payload.get("card_sms"))
            question = " Is there anything else I can help you with today?"
            truth(
                "BOOKING_CONFIRMATION_SPOKEN",
                call_sid=self.call_id,
                card_status=payload.get("card_status") or "none",
                card_sms=payload.get("card_sms") or "not_attempted",
            )
            if sentence.endswith(question):
                return sentence[: -len(question)] + clause + question
            return sentence + clause

        if not confirmed:
            # Still avoid an invented greeting even if the local display time is
            # unexpectedly unavailable; provider IDs prove the write succeeded.
            verb = "confirmed" if status == "booked" else "rescheduled"
            return finish(
                f"You're all set. Your {service} appointment{with_provider} is {verb}. "
                "Is there anything else I can help you with today?"
            )

        try:
            dt = datetime.fromisoformat(str(confirmed).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local = dt.astimezone(self._tz)
            day = f"{local.strftime('%A, %B')} {local.day}"
            spoken_time = local.strftime("%I:%M %p").lstrip("0")
            if spoken_time.endswith(":00 AM") or spoken_time.endswith(":00 PM"):
                spoken_time = spoken_time.replace(":00 ", " ")
        except (TypeError, ValueError):
            verb = "confirmed" if status == "booked" else "rescheduled"
            return finish(
                f"You're all set. Your {service} appointment{with_provider} is {verb}. "
                "Is there anything else I can help you with today?"
            )

        self._awaiting_wrap_up = True
        if status == "rescheduled":
            return finish(
                f"You're all set. Your {service} appointment{with_provider} has been moved to "
                f"{day} at {spoken_time}. "
                "Is there anything else I can help you with today?"
            )
        return finish(
            f"You're all set. Your {service} appointment{with_provider} is confirmed for "
            f"{day} at {spoken_time}. "
            "Is there anything else I can help you with today?"
        )

    async def _send_function_output(
        self,
        call_ref: str | None,
        output: str,
        *,
        cache: bool = True,
        nudge: bool = True,
        restrict_continuation: bool = False,
    ) -> None:
        """Send one `function_call_output`, optionally caching it for replay
        on a duplicate `call_id` and nudging the model to continue.

        The nudge is skipped whenever a response is still open — see the
        comment at the bottom of `_handle_function_call`.

        `restrict_continuation` marks this as a guard-rail REJECTION (ungrounded
        time/earliest, per-response cap, duplicate probe, chain-depth limit):
        the model still needs to say something to the caller, but the
        continuation this triggers must not be free to call a tool again —
        that is exactly the autonomous retry loop this exists to prevent. The
        continuation is sent with `tool_choice: "none"` so it can only speak.
        """
        if cache and call_ref:
            self._call_id_outputs[call_ref] = output
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_ref,
                    "output": output,
                },
            }
        )
        if not nudge:
            return

        response_create: dict[str, Any] = {"type": "response.create"}
        if restrict_continuation:
            response_create["response"] = {"tool_choice": "none"}

        if self._active_response_id is None:
            self._log_tool_continuation(
                "issued",
                reason="restricted_followup" if restrict_continuation else "tool_result_complete",
            )
            await self._send(response_create)
        else:
            # The function call belongs to the response that is still open.
            # Starting another response now would overlap/stack responses,
            # so defer exactly one continuation until response.done.
            if restrict_continuation:
                self._restricted_response_needed_after_tool = True
            else:
                self._response_needed_after_tool = True
            self._log_tool_continuation(
                "deferred",
                reason="restricted_followup" if restrict_continuation else "tool_result_complete",
                active_response_id=self._active_response_id,
            )

    def _log_tool_continuation(self, decision: str, **fields: Any) -> None:
        """Structured log line for every response.create decision this
        module makes, so a runaway tool-continuation chain is diagnosable
        from logs alone: `TOOL_CONTINUATION <decision> call=... turn=... ...`.
        """
        parts = " ".join(f"{key}={value}" for key, value in fields.items())
        logger.info(
            "TOOL_CONTINUATION %s call=%s turn=%d chain_depth=%d %s",
            decision,
            self.call_id,
            self._user_turn_count,
            self._tool_chain_depth_this_turn,
            parts,
        )

    async def _arm_availability_hold(self) -> None:
        """Mute the model while a provider lookup runs.

        Fast lookups answer directly. The delayed task supplies one short status
        line only if the provider is still working after five seconds.
        """
        self._availability_lookup_open = True
        self._availability_speech_interrupted = False
        self._availability_model_response_id = self._active_response_id
        self._muted_availability_response_id = self._active_response_id
        self._availability_hold_response_id = None
        self._availability_hold_done = False
        self._availability_expect_hold = False
        self._pending_availability_speech = None

    async def _record_enhancement(self, status: str, **extra) -> None:
        from app.services.enhancements import record
        try:
            async with AsyncSessionLocal() as db:
                await record(db, self.session, status, **extra)
        except Exception:
            logger.warning("call %s: enhancement analytics unavailable", self.call_id)

    async def _maybe_enhancement(self) -> str | None:
        from app.services.enhancements import prepare, KEY
        if not self.session.entities.get("smart_enhancements_enabled") or self.session.entities.get(KEY):
            return None
        try:
            async with AsyncSessionLocal() as db:
                routing = await _prepare(db, self.session)
                line = await asyncio.wait_for(prepare(db, self.session, routing), timeout=3.0)
                if offer_accepted(self.session) or get_draft(self.session).is_persisted:
                    state = self.session.entities.get(KEY)
                    if state:
                        state["phase"] = "superseded"
                    return None
                if line:
                    self.session.entities[KEY]["presented"] = True
                    await self._record_enhancement("presented")
                return line
        except Exception:
            # Optional reads cannot break the requested booking or cause silent loops.
            state = self.session.entities.get(KEY)
            if state:
                state["phase"] = "skipped"
            logger.info("call %s: enhancement skipped; original booking preserved", self.call_id)
            return None

    async def _speak_availability(self, spoken: str) -> None:
        """Offer once; collect identity after the caller accepts the slot."""
        draft = get_draft(self.session)
        if offer_accepted(self.session):
            await self._confirm_pending_booking_from_caller("yes")
            return
        remember_offer(self.session)
        slots = ([draft.selected_slot] if draft.provider_verified and draft.selected_slot
                 else list(draft.alternative_slots or [])[:3])
        self.session.entities["spoken_booking_choices"] = {
            "revision": draft.draft_revision, "slots": slots,
        }
        self._after_availability_line = None
        self._prompt_caller_name_after_response = False
        suggestion = await self._maybe_enhancement()
        if suggestion:
            # One question: retain the real original slot read-back, defer booking consent.
            spoken = re.split(r"(?i)(?:would you like|shall I|may I|do you want)", spoken)[0].strip()
            spoken = f"{spoken} {suggestion}"
        await self._send_force_message(spoken)

    async def _deliver_authoritative_availability(self, call_ref: str | None, output: str) -> None:
        """Speak a finished Square result and do not let the model continue."""
        spoken = None
        try:
            spoken = json.loads(output).get("spoken")
        except (TypeError, json.JSONDecodeError):
            spoken = None
        await self._send_function_output(call_ref, output, nudge=False)
        model_response = getattr(self, "_availability_model_response_id", None)
        self._availability_model_response_id = None
        self._muted_availability_response_id = None
        if model_response and self._active_response_id == model_response:
            await self._cancel_active_response()
        elif model_response:
            self._cancelled_response_ids.add(model_response)
        if spoken and not self._availability_speech_interrupted:
            hold_still_open = self._availability_expect_hold or (
                self._availability_hold_response_id is not None
                and not self._availability_hold_done
            )
            if hold_still_open:
                self._pending_availability_speech = str(spoken)
            else:
                await self._speak_availability(str(spoken))

    async def _maybe_hold_ack(self) -> None:
        """After five seconds, speak one varied status line, then a soft tone."""
        try:
            await asyncio.sleep(self.HOLD_ACK_DELAY_SECONDS)
        except asyncio.CancelledError:
            return
        if self._ws is None:
            return
        if not self._hold_ack_played_this_turn:
            self._hold_ack_played_this_turn = True
            message = HOLD_ACK_TEXTS[self._hold_ack_index % len(HOLD_ACK_TEXTS)]
            self._hold_ack_index += 1
            if self._availability_lookup_open:
                self._availability_expect_hold = True
            logger.info("HOLD_ACK call=%s variant=%d", self.call_id, self._hold_ack_index)
            await self._send_force_message(message)
        try:
            await asyncio.sleep(self.HOLD_TONE_DELAY_SECONDS)
        except asyncio.CancelledError:
            return
        start = getattr(self, "start_hold_tone", None)
        if start is not None and getattr(self, "_hold_tone_started_turn", None) != self._user_turn_count:
            self._hold_tone_started_turn = self._user_turn_count
            await start()

    async def _invoke_tool_with_hold(self, name: str, handler, raw_args: str) -> str:
        hold_tools = {
            CHECK_AVAILABILITY_TOOL["name"],
            PROPOSE_APPOINTMENT_TOOL["name"],
            CONFIRM_APPOINTMENT_TOOL["name"],
            LOOKUP_APPOINTMENTS_TOOL["name"],
            CANCEL_APPOINTMENT_TOOL["name"],
            LOOKUP_SPA_FACTS_TOOL["name"],
            MANAGE_APPOINTMENT_TOOL["name"],
        }
        if name not in hold_tools:
            return await handler(raw_args)
        ack = asyncio.create_task(self._maybe_hold_ack())
        try:
            if name in {CHECK_AVAILABILITY_TOOL["name"], PROPOSE_APPOINTMENT_TOOL["name"]}:
                try:
                    return await asyncio.wait_for(handler(raw_args), timeout=20)
                except asyncio.TimeoutError:
                    logger.error("AVAILABILITY_TIMEOUT call=%s tool=%s", self.call_id, name)
                    await self._offer_staff_callback()
                    return json.dumps({"status": "lookup_timeout", "available": False, "message": "The availability lookup did not finish. A callback permission question was delivered."})
            return await handler(raw_args)
        finally:
            ack.cancel()
            try:
                await ack
            except asyncio.CancelledError:
                pass
            stop = getattr(self, "stop_hold_tone", None)
            if stop is not None:
                await stop()

    async def _handle_function_call(self, data: dict[str, Any]) -> None:
        name = _first_str(data, "name", "function_name") or ""
        call_ref = _first_str(data, "call_id", "tool_call_id", "id")
        raw_args = _first_str(data, "arguments", "args") or "{}"
        response_id = self._response_id_of(data)
        logger.info(
            "TOOL_CALL received call=%s turn=%d response_id=%s tool_call_id=%s tool=%s args=%s",
            self.call_id, self._user_turn_count, response_id, call_ref, name, raw_args[:500],
        )

        if (
            response_id
            and response_id in self._cancelled_response_ids
            and response_id in self._cancelled_response_output_sent
            and not (name in self._AVAILABILITY_PROBE_TOOLS and self._recover_availability_tool)
        ):
            logger.debug(
                "call %s: discarding additional tool call %r from cancelled response %s",
                self.call_id, name, response_id,
            )
            return

        # A duplicate delivery of a call we already handled. The realtime
        # protocol still expects exactly one function_call_output per
        # call_id, so this replays the cached result instead of silently
        # dropping it — dropping it risks the model waiting on an output it
        # will never see if this was the only delivery it actually noticed.
        # The provider operation itself never runs twice: dispatch is
        # strictly sequential (one event at a time off the socket), so by
        # the time a duplicate can arrive the first call has already
        # finished and its output is cached.
        if call_ref and call_ref in self._call_id_outputs:
            logger.warning("call %s: duplicate tool call_id %r replayed", self.call_id, call_ref)
            await self._send_function_output(
                call_ref, self._call_id_outputs[call_ref], cache=False, nudge=False
            )
            return

        # A function call that arrived for a response we already cancelled
        # (booking-confirmation guard, availability-claim guard, or a
        # caller barge-in) must not still reach the scheduling provider —
        # the conversation already moved on from that response.
        if (
            response_id
            and response_id in self._cancelled_response_ids
            and not (name in self._AVAILABILITY_PROBE_TOOLS and self._recover_availability_tool)
        ):
            logger.warning(
                "call %s: ignoring tool call %r from cancelled response %s",
                self.call_id, name, response_id,
            )
            self._cancelled_response_output_sent.add(response_id)
            await self._send_function_output(
                call_ref,
                json.dumps({
                    "status": "cancelled",
                    "message": "This request was superseded; do not act on it.",
                }),
                nudge=False,
            )
            return
        if name in self._AVAILABILITY_PROBE_TOOLS and self._recover_availability_tool:
            self._recover_availability_tool = False

        from app.services.enhancements import pending as enhancement_pending
        if (enhancement_pending(self.session) or (self.session.entities.get("smart_enhancement") or {}).get("phase") == "checking") and name in self._AVAILABILITY_PROBE_TOOLS | {CONFIRM_APPOINTMENT_TOOL["name"]}:
            await self._send_function_output(call_ref, json.dumps({"status": "awaiting_enhancement_response", "booked": False}), nudge=False)
            return
        if self.session.entities.get("callback_offer_pending") and name in self._AVAILABILITY_PROBE_TOOLS | {CONFIRM_APPOINTMENT_TOOL["name"]}:
            await self._send_function_output(call_ref, json.dumps({"status": "awaiting_callback_consent", "booked": False}), nudge=False)
            return
        if self.session.entities.get("confirmation_text_check_pending") and name in self._AVAILABILITY_PROBE_TOOLS | {
            CONFIRM_APPOINTMENT_TOOL["name"],
            CANCEL_APPOINTMENT_TOOL["name"],
            MANAGE_APPOINTMENT_TOOL["name"],
        }:
            # The next caller turn must answer the recovery question. Never let
            # a model retry, replace, or cancel the write while we are checking
            # whether a provider confirmation may already exist.
            await self._send_function_output(
                call_ref,
                json.dumps({
                    "status": "awaiting_confirmation_text_answer",
                    "booked": False,
                    "message": (
                        "Wait for the caller's yes or no. Do not retry, replace, "
                        "confirm, or cancel the appointment."
                    ),
                }),
                nudge=False,
            )
            return

        if name == PROPOSE_APPOINTMENT_TOOL["name"]:
            try:
                proposal = json.loads(raw_args or "{}")
            except (TypeError, ValueError):
                proposal = {}
            for field in ("guest_name", "caller_name", "caller_email", "preferred_staff"):
                if str(proposal.get(field) or "").strip().casefold() in {"", "none", "null", "undefined"}:
                    proposal.pop(field, None)
            raw_args = json.dumps(proposal)
            rescheduling = (
                proposal.get("operation") == "reschedule" or proposal.get("appointment_id")
                or get_draft(self.session).operation_mode == "reschedule"
                or _RESCHEDULE_REQUEST_RE.search(self._last_user_utterance() or "")
            )
            if rescheduling and not proposal.get("requested_start_iso") and not proposal.get("earliest"):
                await self._send_function_output(call_ref, json.dumps({"status": "missing_new_time", "booked": False}), nudge=False)
                self._response_needed_after_tool = False
                self._restricted_response_needed_after_tool = False
                self._pending_forced_tool_message = None
                pending = get_draft(self.session)
                pending.operation_mode = "reschedule"
                save_draft(self.session, pending)
                previous = self.session.entities.get("reschedule_time_question_turn")
                if previous is None:
                    self.session.entities["reschedule_time_question_turn"] = self._user_turn_count
                    await self._cancel_active_response()
                    await self._send_force_message("What day and time would you like to move your appointment to?")
                elif previous != self._user_turn_count:
                    await self._offer_staff_callback()
                return
            if rescheduling:
                self.session.entities.pop("reschedule_time_question_turn", None)

        # Hard ceiling on tool round-trips within one caller turn, independent
        # of every guard above. Even if a guard were somehow bypassed, this
        # bounds the damage instead of relying on any single check being
        # perfect. Counts every real attempt (any tool), not just probes.
        self._tool_chain_depth_this_turn += 1
        if self._tool_chain_depth_this_turn > self.MAX_TOOL_CHAIN_DEPTH_PER_TURN:
            logger.error(
                "TOOL_CONTINUATION blocked call=%s turn=%d tool=%s reason=chain_depth_limit "
                "depth=%d limit=%d",
                self.call_id, self._user_turn_count, name,
                self._tool_chain_depth_this_turn, self.MAX_TOOL_CHAIN_DEPTH_PER_TURN,
            )
            await self._send_function_output(
                call_ref,
                json.dumps({
                    "status": "tool_chain_limit",
                    "available": False,
                    "message": (
                        "Too many attempts this turn. Stop calling tools and ask the "
                        "caller a clarifying question instead."
                    ),
                }),
                nudge=False,
            )
            await self._offer_staff_callback()
            return

        if name in self._AVAILABILITY_PROBE_TOOLS:
            try:
                parsed_args = json.loads(raw_args or "{}")
            except (TypeError, ValueError):
                parsed_args = {}
            self._apply_caller_service_durations(parsed_args)
            cancelled = self._cancelled_slot_reference()
            if parsed_args.get("requested_services"):
                parsed_args["service_description"] = " + ".join(parsed_args["requested_services"])
                raw_args = json.dumps(parsed_args)
            if cancelled:
                parsed_args.setdefault("requested_start_iso", cancelled["start_iso"])
                parsed_args.setdefault("service_description", cancelled.get("service"))
                parsed_args["earliest"] = False
                raw_args = json.dumps(parsed_args)

            # A calendar day plus a service is enough to continue the
            # conversation, but it is not permission for the model to invent
            # one exact timestamp or to dump an entire day of results. Ask for
            # the caller's preferred part of day, then perform one bounded
            # provider search that returns at most three real openings.
            utterance = self._last_user_utterance() or ""
            approximate_hour = re.search(
                rf"\b(?:around|about|near)\s+(?:[01]?\d|2[0-3]|{_TIME_WORD})\b",
                utterance,
                re.IGNORECASE,
            )
            no_spoken_time = (
                _extract_clock_time(utterance) is None
                and _extract_day_part(utterance) is None
                and approximate_hour is None
            )
            pending_window = self.session.entities.get("requested_availability_window")
            caller_named_day = bool(_DATE_MENTION_RE.search(utterance))
            has_service = bool(
                parsed_args.get("service_description")
                or parsed_args.get("requested_services")
                or get_draft(self.session).service_description
            )
            if no_spoken_time and has_service and (caller_named_day or pending_window):
                if caller_named_day:
                    now = self._now()
                    named_date, source = self._spoken_date(utterance, now.date())
                    if named_date is not None and source != "none":
                        day_start = datetime.combine(named_date, dt_time.min, tzinfo=self._tz)
                        day_end = datetime.combine(named_date + timedelta(days=1), dt_time.min, tzinfo=self._tz)
                        self.session.entities["requested_availability_window"] = [
                            max(day_start, now).isoformat(), day_end.isoformat()
                        ]
                await self._send_function_output(
                    call_ref,
                    json.dumps({
                        "status": "missing_day_part",
                        "available": False,
                        "message": "Would you prefer a morning, afternoon, or evening appointment?",
                    }),
                    nudge=False,
                )
                if self.session.entities.get("day_part_question_turn") != self._user_turn_count:
                    self.session.entities["day_part_question_turn"] = self._user_turn_count
                    await self._cancel_active_response()
                    await self._send_force_message("Would you prefer morning, afternoon, or evening?")
                return
            spoken_window = self._spoken_day_part_window()
            if spoken_window is not None and name in {
                CHECK_AVAILABILITY_TOOL["name"],
                PROPOSE_APPOINTMENT_TOOL["name"],
            }:
                await self._offer_spoken_window(call_ref, spoken_window, parsed_args)
                return

            spoken_start = self._spoken_exact_start()
            if spoken_start is not None:
                local_iso = spoken_start.strftime("%Y-%m-%dT%H:%M:%S")
                if parsed_args.get("requested_start_iso") != local_iso:
                    logger.info(
                        "call %s: using caller wording %s instead of model time %s",
                        self.call_id,
                        local_iso,
                        parsed_args.get("requested_start_iso"),
                    )
                parsed_args["requested_start_iso"] = local_iso
                parsed_args["earliest"] = False
                raw_args = json.dumps(parsed_args)

            is_earliest = bool(parsed_args.get("earliest"))
            requested_start = parsed_args.get("requested_start_iso")

            # Same semantically-equivalent probe already rejected this turn
            # (e.g. an offset-qualified duplicate of the same business-local
            # instant) must not be allowed to run the exact same rejection
            # (and its follow-up) over and over — this is the actual
            # loop-breaker, independent of whether the model respects the
            # speech-only continuation below.
            signature = (name, json.dumps({
                "time": self._time_key(requested_start) if not is_earliest else "earliest",
                "service": parsed_args.get("service_description"),
                "staff": parsed_args.get("preferred_staff"),
                "revision": get_draft(self.session).draft_revision,
            }, sort_keys=True))
            if signature in self._rejected_probe_signatures_this_turn:
                logger.error(
                    "TOOL_CONTINUATION blocked call=%s turn=%d tool=%s reason=duplicate_rejected_probe "
                    "args=%s",
                    self.call_id, self._user_turn_count, name, signature,
                )
                await self._send_function_output(
                    call_ref,
                    json.dumps({
                        "status": "duplicate_probe",
                        "available": False,
                        "message": (
                            "I still need a specific date and time before I can check "
                            "that appointment."
                        ),
                    }),
                    nudge=False,
                )
                await self._offer_staff_callback()
                return

            if is_earliest and not self._caller_requested_earliest():
                logger.warning(
                    "call %s: refusing ungrounded earliest search; latest caller turn=%r",
                    self.call_id,
                    self._last_user_utterance(),
                )
                self._rejected_probe_signatures_this_turn.add(signature)
                await self._send_function_output(
                    call_ref,
                    json.dumps({
                        "status": "ungrounded_earliest",
                        "available": False,
                        "message": (
                            "Which date and time would you prefer?"
                        ),
                    }),
                    restrict_continuation=True,
                )
                return

            # The core scheduler rule: the model placing a timestamp in tool
            # arguments is NOT evidence the caller asked for it. An exact-time
            # probe must be grounded in a fresh caller turn or a previously
            # offered/pinned authoritative slot — checked here, independent
            # of `response_id`, so a NEW response cannot re-grant permission
            # to keep guessing (the per-response cap below is a secondary
            # safety net, not the correctness rule).
            if not is_earliest and requested_start and not self._is_time_grounded(requested_start):
                logger.warning(
                    "call %s: refusing ungrounded exact-time probe %r (no new caller "
                    "turn, no matching offered slot)",
                    self.call_id, requested_start,
                )
                self._rejected_probe_signatures_this_turn.add(signature)
                attempts = self._grounding_rejections.get(signature, 0) + 1
                self._grounding_rejections[signature] = attempts
                if attempts >= 2:
                    await self._send_function_output(call_ref, json.dumps({
                        "status": "needs_staff_help", "booked": False,
                        "message": "The requested appointment needs clarification. Nothing has been booked."
                    }), nudge=False)
                    await self._offer_staff_callback()
                    return
                await self._send_function_output(
                    call_ref,
                    json.dumps({
                        "status": "ungrounded_time",
                        "available": False,
                        "message": (
                            "Which date and time would you like? Please include AM or PM."
                        ),
                    }),
                    restrict_continuation=True,
                )
                return

            # This probe is grounded (or an explicit earliest search). For an
            # exact-time probe, remember THIS instant before consuming the fresh
            # caller turn. A retry of the same instant remains valid; a different
            # timestamp still cannot ride along on the same turn.
            if not is_earliest and requested_start:
                self._remember_grounded_exact_time(requested_start)
            self._last_probe_turn_count = self._user_turn_count

        if name in self._AVAILABILITY_PROBE_TOOLS and response_id:
            probes = self._availability_probes_by_response.get(response_id, 0) + 1
            self._availability_probes_by_response[response_id] = probes
            if probes > self.MAX_AVAILABILITY_PROBES_PER_RESPONSE:
                logger.warning(
                    "call %s: refusing availability probe #%d in one response (%s)",
                    self.call_id, probes, response_id,
                )
                self._rejected_probe_signatures_this_turn.add(signature)
                await self._send_function_output(
                    call_ref,
                    json.dumps({
                        "status": "too_many_attempts",
                        "available": False,
                        "message": (
                            "Which specific date and time would you like, or would you like "
                            "the earliest opening?"
                        ),
                    }),
                    restrict_continuation=True,
                )
                return

        handlers = {
            CHECK_AVAILABILITY_TOOL["name"]: self._run_check_availability,
            PROPOSE_APPOINTMENT_TOOL["name"]: self._run_propose_appointment,
            CONFIRM_APPOINTMENT_TOOL["name"]: self._run_confirm_appointment,
            LOOKUP_APPOINTMENTS_TOOL["name"]: self._run_lookup_appointments,
            CANCEL_APPOINTMENT_TOOL["name"]: self._run_cancel_appointment,
            NEW_APPOINTMENT_TOOL["name"]: self._run_start_new_appointment,
            MANAGE_APPOINTMENT_TOOL["name"]: self._run_manage_appointment,
            LOOKUP_SPA_FACTS_TOOL["name"]: self._run_lookup_spa_facts,
            REQUEST_CALLBACK_TOOL["name"]: self._run_request_callback,
        }
        handler = handlers.get(name)
        if handler is None:
            logger.warning("call %s: unknown tool %r requested", self.call_id, name)
            output = f"Tool {name!r} is not available."
        else:
            output = await self._invoke_tool_with_hold(name, handler, raw_args)

        if name == CHECK_AVAILABILITY_TOOL["name"] or (
            name == PROPOSE_APPOINTMENT_TOOL["name"]
            and '"spoken":' in output
            and json.loads(output).get("spoken")
        ):
            await self._deliver_authoritative_availability(call_ref, output)
            return

        # Successful booking confirmation is authoritative and terminal: speak
        # an exact backend-derived line rather than asking Grok to invent the
        # next turn (which has been observed to restart with "hello/welcome").
        forced_followup = self._authoritative_tool_followup(name, output)
        if forced_followup == "":
            await self._send_function_output(call_ref, output, nudge=False)
            return
        if forced_followup is None and name == CONFIRM_APPOINTMENT_TOOL["name"]:
            try:
                confirm_payload = json.loads(output)
            except (TypeError, json.JSONDecodeError):
                confirm_payload = {}
            confirm_message = str(confirm_payload.get("message") or "")
            if "has not given their name" in confirm_message:
                forced_followup = CALLER_NAME_QUESTION
                self.session.entities["awaiting_caller_name"] = True
            elif CARD_ON_FILE_POLICY in confirm_message:
                forced_followup = CARD_ON_FILE_POLICY
                self.session.entities["card_policy_explained"] = True
            elif CARD_LINK_PERMISSION_QUESTION in confirm_message:
                forced_followup = CARD_LINK_PERMISSION_QUESTION
        if forced_followup is not None:
            await self._send_function_output(call_ref, output, nudge=False)
            if forced_followup == BOOKING_CONFIRMATION_TEXT_QUESTION:
                await self._persist_session()
            if self._active_response_id is None:
                logger.info(
                    "call %s: authoritative %s result -> force_message confirmation",
                    self.call_id,
                    name,
                )
                await self._send_force_message(
                    forced_followup,
                    protect_playback=(
                        name == CONFIRM_APPOINTMENT_TOOL["name"]
                        and forced_followup != BOOKING_CONFIRMATION_TEXT_QUESTION
                    ),
                )
            else:
                self._pending_forced_tool_message = forced_followup
                logger.info(
                    "call %s: authoritative %s result ready; deferring force_message until response %s closes",
                    self.call_id,
                    name,
                    self._active_response_id,
                )
            return

        # Non-terminal tool results still need an explicit model continuation,
        # but only after the response that requested the tool has closed.
        await self._send_function_output(call_ref, output)

    # ------------------------------------------------------------ event loop
    async def _dispatch(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        data = event.get("data") if isinstance(event.get("data"), dict) else event

        if etype in _AUDIO_DELTA_EVENTS:
            await self._note_greeting_audio_started(
                self._response_id_of(event) or self._response_id_of(data)
            )
            return

        if etype in _SPEECH_STARTED_EVENTS:
            self._caller_speaking = True
            self._cancel_confirmation_wait()
            self._start_turn()
            return

        if etype == "input_audio_buffer.speech_stopped":
            self._mark_timing("VAD END")
            return

        if etype in _CALLER_TRANSCRIPT_UPDATED:
            self._mark_timing("STT")
            text = _first_str(data, "transcript", "text", "delta")
            if text:
                self._pending_caller = text
            return

        if etype in _CALLER_TRANSCRIPT_DONE:
            self._caller_speaking = False
            self._cancel_confirmation_wait()
            self._mark_timing("STT")
            text = _first_str(data, "transcript", "text")
            if text:
                self._pending_caller = text
            caller_text = self._pending_caller.strip()
            self._flush_caller_turn()
            if caller_text and capture_caller_name_answer(self.session, caller_text):
                self._caller_name_question_sent = False
                draft = get_draft(self.session)
                if (
                    draft.selected_slot
                    and draft.provider_verified
                    and self.session.booking_status not in {"booked", "rescheduled"}
                ):
                    self.session.booking_status = "awaiting_confirmation"
                    if offer_accepted(self.session):
                        await self._confirm_pending_booking_from_caller("yes")
                        await self._persist_session()
                        return
                    spoken = authoritative_availability_speech(self.session, self._tz)
                    if spoken:
                        await self._speak_availability(spoken)
                        self._mark_pending_booking_read_back(spoken)
                        self._after_availability_line = self._next_collection_line()
                        self._prompt_caller_name_after_response = (
                            self._after_availability_line is not None
                        )
                await self._persist_session()
                return
            # Once a provider-checked draft has been explicitly read back, an
            # unqualified caller affirmation commits it here. This makes the
            # final write independent of whether the realtime model remembers
            # to issue confirm_appointment on its next turn.
            if (
                caller_text
                and self._awaiting_wrap_up
                and _caller_is_finished(caller_text)
            ):
                if getattr(self, "_protect_playback", False):
                    self._deferred_wrap_up = True
                    await self._persist_session()
                    return
                self._awaiting_wrap_up = False
                await self._send_force_message(
                    "Perfect. We look forward to seeing you. Have a great day!"
                )
                await self._persist_session()
                return
            if caller_text and await self._confirm_pending_booking_from_caller(caller_text):
                await self._persist_session()
                return
            await self._persist_session()
            return

        if etype in _AGENT_TRANSCRIPT_DELTA:
            self._mark_timing("LLM FIRST TOKEN")
            # The caller's turn ended the moment the agent began replying.
            self._flush_caller_turn()
            delta = _first_str(data, "delta", "transcript", "text")
            if delta:
                candidate_text = self._pending_agent + delta
                if re.search(r"\b(?:i (?:will not|won't|wont)|do not|don't) (?:give|offer|suggest) (?:any )?(?:other|alternative) times", candidate_text, re.IGNORECASE):
                    self._pending_agent = ""
                    await self._cancel_active_response()
                    await self._send_force_message("What would you like to do?")
                    return
                if (
                    self._should_cancel_restart_greeting(event)
                    and self._looks_like_session_restart_greeting(candidate_text)
                ):
                    await self._suppress_restart_greeting(
                        candidate_text, reason="already_greeted"
                    )
                    return
                if (
                    not self._booking_is_persisted
                    and looks_like_unverified_booking_success(candidate_text)
                ):
                    await self._replace_blocked_confirmation_speech(candidate_text)
                    return
                if self._is_unauthorized_availability_claim(candidate_text):
                    await self._block_unverified_availability(candidate_text)
                    return
                self._pending_agent = candidate_text
            return

        if etype in _AGENT_TRANSCRIPT_DONE:
            self._flush_caller_turn()
            # A `.done` carrying the full text supersedes the accumulated
            # deltas; otherwise the deltas are all we have.
            text = _first_str(data, "transcript", "text")
            if text:
                if self._blocked_unverified_success:
                    self._blocked_unverified_success = False
                elif (
                    self._should_cancel_restart_greeting(event)
                    and self._looks_like_session_restart_greeting(text)
                ):
                    await self._suppress_restart_greeting(text, reason="already_greeted")
                elif (
                    not self._booking_is_persisted
                    and looks_like_unverified_booking_success(text)
                ):
                    await self._replace_blocked_confirmation_speech(text)
                elif self._is_unauthorized_availability_claim(text):
                    await self._block_unverified_availability(text)
                else:
                    self._pending_agent = text
            readback_text = self._pending_agent or text
            if readback_text:
                self._mark_pending_booking_read_back(readback_text)
            self._flush_agent_turn()
            await self._persist_session()
            return

        if etype in _FUNCTION_CALL_DONE:
            self._flush_caller_turn()
            await self._handle_function_call(data)
            return

        # Observed on a live socket: server errors can arrive as a bare
        # {"error": "..."} frame with no `type` at all, so keying only on
        # type == "error" would file real failures under _UNHANDLED.
        if etype == "error" or (not etype and "error" in event):
            message = _first_str(data, "message", "error", "code") or json.dumps(data)[:300]
            if "cancellation failed: no active response found" in message.lower():
                # Generation can finish on the server before its response.done
                # reaches us. A cancel in that interval is benign: do not replay
                # the greeting or disturb a newer response when the error arrives.
                logger.info("call %s: response cancellation raced with completion", self.call_id)
                return
            logger.error("call %s: xAI realtime error: %s", self.call_id, message)
            if self.session.greeting_sent:
                truth(
                    "CALL_GREETING_SUPPRESSED",
                    call_sid=self.call_id,
                    reason="already_greeted",
                    error=message[:120],
                )
            elif self._startup_greeting_open():
                truth(
                    "CALL_GREETING_FAILED",
                    call_sid=self.call_id,
                    reason="xai_error",
                    error=message[:120],
                )
                await self._greet_via_model()
            else:
                truth(
                    "CALL_GREETING_SUPPRESSED",
                    call_sid=self.call_id,
                    reason="already_greeted",
                    error=message[:120],
                )
            return

        if etype == "session.created":
            self._xai_session_ready.set()
            # The only observability there is on the voice setting: xAI accepts
            # any voice string without validating it and never echoes it back,
            # so a typo (or a name from another vendor's catalogue) silently
            # leaves the caller listening to the server default instead of the
            # configured persona. Log both so the mismatch is greppable.
            requested = resolve_xai_voice(
                self.session.entities.get("xai_voice") or settings.XAI_VOICE_ID
            )
            server_default = (event.get("session") or {}).get("voice")
            server_model = (event.get("session") or {}).get("model") or "server-selected"
            logger.info(
                "call %s: xAI session created voice=%s realtime_model=%s",
                self.call_id,
                requested,
                server_model,
            )
            if server_default and requested != server_default:
                logger.info(
                    "call %s: requested voice %r (server default %r). xAI does not "
                    "validate voice names — if the caller hears the wrong voice, "
                    "this is the first thing to check.",
                    self.call_id, requested, server_default,
                )
            return

        if etype in _BENIGN_EVENTS:
            if etype == "response.created":
                self._active_response_id = self._response_id_of(data) or "unknown"
                self._blocked_unverified_success = False
                if self._availability_expect_hold and self._active_response_id != self._availability_model_response_id:
                    self._availability_hold_response_id = self._active_response_id
                    self._availability_expect_hold = False
                if self._prompt_caller_name_after_response:
                    self._caller_name_prompt_after_id = self._active_response_id
                    self._prompt_caller_name_after_response = False
                if self._greeting_pending and not self.session.greeting_sent:
                    self._greeting_response_id = self._active_response_id
                    truth(
                        "CALL_GREETING_RESPONSE_CREATED",
                        call_sid=self.call_id,
                        response_id=self._greeting_response_id,
                    )
            elif etype == "response.done":
                event_response_id = self._response_id_of(data)
                # The short hold response can finish after xAI has already
                # declared a newer response active.  Process that completion
                # before the stale-response guard so a completed Square lookup
                # cannot remain buffered in silence indefinitely.
                if (
                    event_response_id
                    and event_response_id == self._availability_hold_response_id
                    and event_response_id != self._active_response_id
                ):
                    self._availability_hold_done = True
                    pending_availability = self._pending_availability_speech
                    self._pending_availability_speech = None
                    if pending_availability and not self._availability_speech_interrupted:
                        await self._speak_availability(pending_availability)
                    return
                if (event_response_id and self._active_response_id
                        and event_response_id != self._active_response_id):
                    return
                self._mark_timing("LLM COMPLETE")
                self._flush_agent_turn()
                self._finish_turn()
                finished_response_id = self._active_response_id
                greeting_failed = (
                    self._greeting_pending
                    and not self.session.greeting_sent
                    and not self._greeting_got_audio
                    and (
                        not self._greeting_response_id
                        or finished_response_id == self._greeting_response_id
                        or self._response_id_of(data) == self._greeting_response_id
                    )
                )
                self._active_response_id = None
                finished = self._response_id_of(data) or finished_response_id
                if finished and finished == self._availability_hold_response_id:
                    self._availability_hold_done = True
                    pending_availability = self._pending_availability_speech
                    self._pending_availability_speech = None
                    if pending_availability and not self._availability_speech_interrupted:
                        await self._speak_availability(pending_availability)
                        return
                if finished and finished == self._caller_name_prompt_after_id:
                    self._caller_name_prompt_after_id = None
                    line = self._after_availability_line
                    self._after_availability_line = None
                    if line:
                        await self._speak_collection_line(line)
                        return
                if greeting_failed:
                    truth(
                        "CALL_GREETING_FAILED",
                        call_sid=self.call_id,
                        reason="no_audio",
                        response_id=self._greeting_response_id,
                    )
                    if self._startup_greeting_open():
                        await self._greet_via_model()
                    return
                if self._pending_forced_tool_message is not None:
                    forced_message = self._pending_forced_tool_message
                    self._pending_forced_tool_message = None
                    # Defensive: a deterministic terminal follow-up replaces any
                    # generic model continuation for the same tool result.
                    self._response_needed_after_tool = False
                    self._restricted_response_needed_after_tool = False
                    logger.info(
                        "call %s: response %s closed after authoritative tool result -> sending force_message",
                        self.call_id,
                        finished_response_id,
                    )
                    await self._send_force_message(
                        forced_message,
                        protect_playback="anything else I can help" in forced_message,
                    )
                elif self._restricted_response_needed_after_tool:
                    self._restricted_response_needed_after_tool = False
                    self._response_needed_after_tool = False
                    self._log_tool_continuation(
                        "issued", reason="restricted_followup_deferred",
                        closed_response_id=finished_response_id,
                    )
                    await self._send({"type": "response.create", "response": {"tool_choice": "none"}})
                elif self._response_needed_after_tool:
                    self._response_needed_after_tool = False
                    self._log_tool_continuation(
                        "issued", reason="tool_result_complete_deferred",
                        closed_response_id=finished_response_id,
                    )
                    await self._send({"type": "response.create"})
            return

        logger.debug("call %s: _UNHANDLED %s %s", self.call_id, etype, json.dumps(event)[:400])

    async def run(self) -> None:
        headers = {"Authorization": f"Bearer {settings.XAI_API_KEY}"}
        try:
            async with ws_connect(self._url, additional_headers=headers) as ws:
                self._ws = ws
                await self._configure()
                await self._await_xai_session_ready()
                await self._greet()
                logger.info("call %s: realtime session open", self.call_id)

                while True:
                    remaining = self._deadline - time.monotonic()
                    if remaining <= 0:
                        logger.warning("call %s: exceeded max call duration", self.call_id)
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        logger.warning("call %s: exceeded max call duration", self.call_id)
                        break
                    except websockets.exceptions.ConnectionClosed:
                        break

                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.debug("call %s: non-JSON frame dropped", self.call_id)
                        continue
                    # Provider tools must not stall websocket receive / VAD / barge-in.
                    etype = event.get("type") if isinstance(event, dict) else None
                    if etype in _FUNCTION_CALL_DONE:
                        task = asyncio.create_task(self._dispatch(event))
                        self._inflight_tools.add(task)
                        task.add_done_callback(self._inflight_tools.discard)
                    else:
                        await self._dispatch(event)
        except Exception:
            logger.exception("call %s: realtime session failed", self.call_id)
        finally:
            self._flush_caller_turn()
            self._flush_agent_turn()
            await self._finalize()

    # -------------------------------------------------------------- teardown
    async def _finalize(self) -> None:
        """Persist transcript, summary and status — the <Gather> flow's
        /voice/status handler does the same job for the Twilio path."""
        self._cancel_confirmation_wait()
        self._cancel_greeting_watchdog()
        mark_call_ended(self.session)
        store = self._store()
        await store.save(self.session)
        await store.end(self.call_id)

        transcript = self.session.transcript_text
        analysis = None
        if self.session.history:
            analysis = await grok_service.analyze_call(self.session)

        try:
            async with AsyncSessionLocal() as db:
                call_log = (
                    await db.execute(
                        select(CallLog).where(
                            CallLog.twilio_call_sid == self._call_log_sid
                        )
                    )
                ).scalar_one_or_none()
                if call_log is None:
                    logger.warning("call %s: no CallLog row to finalize", self.call_id)
                    return
                call_log.status = CallStatus.COMPLETED
                call_log.ended_at = datetime.now(timezone.utc)
                if call_log.started_at:
                    call_log.duration_seconds = int(
                        (call_log.ended_at - call_log.started_at).total_seconds()
                    )
                call_log.transcript = transcript or None
                call_log.ai_analysis = {
                    **(call_log.ai_analysis or {}),
                    "booking_outcome": self.session.booking_status
                    if self.session.booking_status != "none"
                    else "no_booking",
                    "linked_appointment_id": self.session.appointment_id,
                    "sensitive_health_request": bool(self.session.entities.get("sensitive_health_request")),
                    "medical_workflow_enabled": False,
                }
                if analysis:
                    call_log.ai_summary = analysis.summary
                    call_log.ai_analysis.update(analysis.model_dump())
                    if call_log.direction is CallDirection.INBOUND:
                        call_log.primary_language = primary_caller_language(
                            self.session, analysis.primary_language
                        )
                        await persist_caller_identity(
                            db, self.session, analysis.caller_name, analysis.caller_email
                        )
                await db.commit()
            logger.info(
                "call %s: finalized (%d chars of transcript)", self.call_id, len(transcript)
            )
        except Exception:
            logger.exception("call %s: failed to persist call log", self.call_id)


async def drive_call(call_id: str, session: CallSession) -> None:
    """Entry point used as a FastAPI background task by the incoming webhook."""
    await XAIVoiceSession(call_id, session).run()
