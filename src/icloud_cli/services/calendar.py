"""Calendar service for icloud-cli.

Provides CRUD operations for iCloud Calendar events via pyicloud.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from dateutil import parser as dateutil_parser
from pyicloud import PyiCloudService
from pyicloud.services.calendar import EventObject

from icloud_cli.config import Config


def _parse_date(date_str: str | None, default: datetime | None = None) -> datetime | None:
    """Parse a date string with natural language support."""
    if date_str is None:
        return default

    # Natural language shortcuts
    now = datetime.now()
    shortcuts = {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "tomorrow": (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
        "yesterday": (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
    }

    if date_str.lower() in shortcuts:
        return shortcuts[date_str.lower()]

    try:
        return dateutil_parser.parse(date_str)
    except (ValueError, TypeError):
        return default


def _format_datetime(dt: Any) -> str:
    """Format a datetime-like object for display."""
    if dt is None:
        return ""
    if isinstance(dt, str):
        try:
            dt = dateutil_parser.parse(dt)
        except (ValueError, TypeError):
            return dt
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M")
    if isinstance(dt, (int, float)):
        # Unix timestamp in milliseconds
        try:
            return datetime.fromtimestamp(dt / 1000).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            return str(dt)
    if isinstance(dt, (list, tuple)) and len(dt) >= 6:
        # iCloud date format: [YYYYMMDD, year, month, day, hour, minute, ...]
        try:
            year, month, day, hour, minute = int(dt[1]), int(dt[2]), int(dt[3]), int(dt[4]), int(dt[5])
            return datetime(year, month, day, hour, minute).strftime("%Y-%m-%d %H:%M")
        except (ValueError, IndexError, TypeError):
            return str(dt)
    return str(dt)


class CalendarService:
    """Manages iCloud Calendar events."""

    def __init__(self, api: PyiCloudService, config: Config):
        self.api = api
        self.config = config

    def list_events(
        self, from_date: str | None = None, to_date: str | None = None
    ) -> list[dict[str, Any]]:
        """List calendar events within a date range.

        Args:
            from_date: Start date string (default: today).
            to_date: End date string (default: 7 days from start).

        Returns:
            List of event dictionaries.
        """
        now = datetime.now()
        start = _parse_date(from_date, default=now.replace(hour=0, minute=0, second=0))
        end = _parse_date(to_date, default=start + timedelta(days=7))

        try:
            events = self.api.calendar.get_events(from_dt=start, to_dt=end)
        except Exception as e:
            from icloud_cli.output import error
            error(f"Failed to fetch events: {e}")
            return []

        result = []
        for event in events:
            result.append({
                "id": event.get("guid", ""),
                "title": event.get("title", "Untitled"),
                "start": _format_datetime(
                    event.get("startDate") or event.get("localStartDate")
                ),
                "end": _format_datetime(
                    event.get("endDate") or event.get("localEndDate")
                ),
                "calendar": event.get("pGuid", ""),
                "location": event.get("location", ""),
                "all_day": event.get("allDay", False),
            })

        # Sort by start date
        result.sort(key=lambda x: x.get("start", ""))
        return result

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        """Get detailed info for a specific event.

        Args:
            event_id: The event GUID.

        Returns:
            Event dictionary or None if not found.
        """
        now = datetime.now()
        start = now - timedelta(days=365)
        end = now + timedelta(days=365)

        try:
            events = self.api.calendar.get_events(from_dt=start, to_dt=end)
        except Exception:
            return None

        for event in events:
            if event.get("guid") == event_id:
                return {
                    "id": event.get("guid", ""),
                    "title": event.get("title", "Untitled"),
                    "start": _format_datetime(
                        event.get("startDate") or event.get("localStartDate")
                    ),
                    "end": _format_datetime(
                        event.get("endDate") or event.get("localEndDate")
                    ),
                    "calendar": event.get("pGuid", ""),
                    "location": event.get("location", ""),
                    "description": event.get("description", ""),
                    "all_day": event.get("allDay", False),
                    "url": event.get("url", ""),
                }

        return None

    def add_event(
        self,
        title: str,
        start: str,
        end: str,
        calendar_name: str | None = None,
        location: str | None = None,
        description: str | None = None,
    ) -> bool:
        """Add a new calendar event.

        Args:
            title: Event title.
            start: Start datetime string.
            end: End datetime string.
            calendar_name: Target calendar name (uses default if None).
            location: Event location.
            description: Event description/notes.

        Returns:
            True if event was created successfully.
        """
        start_dt = _parse_date(start)
        end_dt = _parse_date(end)

        if not start_dt or not end_dt:
            from icloud_cli.output import error
            error("Invalid date format. Use YYYY-MM-DD HH:MM or natural language.")
            return False

        try:
            # Resolve calendar GUID
            pguid = self._resolve_calendar_guid(calendar_name)

            # Build EventObject
            event = EventObject(
                pguid=pguid,
                title=title,
                start_date=start_dt,
                end_date=end_dt,
                location=location or "",
            )

            self.api.calendar.add_event(event)
            return True
        except Exception as e:
            from icloud_cli.output import error
            error(f"Failed to create event: {e}")
            return False

    def update_event(
        self,
        event_id: str,
        title: str | None = None,
        start: str | None = None,
        end: str | None = None,
        location: str | None = None,
    ) -> bool:
        """Update an existing calendar event.

        Only fields that are provided (not None) are changed; the rest keep
        their current values. Editing works by re-POSTing an EventObject with
        the same guid — Apple's CalDAV-style API treats this as an update.

        Args:
            event_id: The event GUID.
            title: New title (optional).
            start: New start datetime string (optional).
            end: New end datetime string (optional).
            location: New location (optional).

        Returns:
            True if event was updated successfully.
        """
        try:
            # Find the event to get its pguid and current values
            current = self.get_event(event_id)
            if not current:
                from icloud_cli.output import error
                error(f"Event not found: {event_id}")
                return False

            pguid = current.get("calendar", "")

            # Fetch raw event detail to get precise start/end datetimes
            # (get_event returns formatted strings, not datetime objects)
            detail = self.api.calendar.get_event_detail(pguid, event_id, as_obj=False)

            # Parse existing start/end from Apple's date list format
            def _parse_apple_date(ad: Any) -> datetime | None:
                if isinstance(ad, (list, tuple)) and len(ad) >= 6:
                    try:
                        return datetime(
                            int(ad[1]), int(ad[2]), int(ad[3]),
                            int(ad[4]), int(ad[5]),
                        )
                    except (ValueError, IndexError, TypeError):
                        return None
                return None

            start_dt = _parse_apple_date(detail.get("startDate"))
            end_dt = _parse_apple_date(detail.get("endDate"))

            if not start_dt or not end_dt:
                from icloud_cli.output import error
                error(f"Could not parse existing event dates for: {event_id}")
                return False

            # Apply overrides
            new_title = title if title is not None else current.get("title", "Untitled")
            new_location = (
                location if location is not None else current.get("location", "")
            )

            if start is not None:
                parsed_start = _parse_date(start)
                if not parsed_start:
                    from icloud_cli.output import error
                    error(f"Invalid start date format: {start}")
                    return False
                start_dt = parsed_start

            if end is not None:
                parsed_end = _parse_date(end)
                if not parsed_end:
                    from icloud_cli.output import error
                    error(f"Invalid end date format: {end}")
                    return False
                end_dt = parsed_end

            if start_dt >= end_dt:
                from icloud_cli.output import error
                error("Start date must be before end date.")
                return False

            # Build EventObject with the SAME guid — triggers update, not create
            event = EventObject(
                pguid=pguid,
                title=new_title,
                start_date=start_dt,
                end_date=end_dt,
                location=new_location,
                guid=event_id,
            )

            self.api.calendar.add_event(event)
            return True
        except Exception as e:
            from icloud_cli.output import error
            error(f"Failed to update event: {e}")
            return False

    def delete_event(self, event_id: str) -> bool:
        """Delete a calendar event by ID.

        Args:
            event_id: The event GUID.

        Returns:
            True if event was deleted successfully.
        """
        try:
            # Find the event to get its pguid (calendar GUID)
            event_detail = self.get_event(event_id)
            if not event_detail:
                from icloud_cli.output import error
                error(f"Event not found: {event_id}")
                return False

            pguid = event_detail.get("calendar", "")
            event = EventObject(
                pguid=pguid,
                title=event_detail.get("title", ""),
                guid=event_id,
            )

            self.api.calendar.remove_event(event)
            return True
        except Exception as e:
            from icloud_cli.output import error
            error(f"Failed to delete event: {e}")
            return False

    def _resolve_calendar_guid(self, calendar_name: str | None) -> str:
        """Resolve a calendar name to its GUID. Uses default calendar if None or not found.

        Args:
            calendar_name: Name of the calendar, or None for default.

        Returns:
            The calendar GUID.
        """
        calendars = self.api.calendar.get_calendars()
        if not calendars:
            return ""

        # If no name specified, use the first calendar (default)
        if not calendar_name:
            for cal in calendars:
                if cal.get("isDefault"):
                    return cal.get("guid", "")
            return calendars[0].get("guid", "") if calendars else ""

        # Find by name
        for cal in calendars:
            if cal.get("title", "") == calendar_name:
                return cal.get("guid", "")

        # Not found — fall back to default
        for cal in calendars:
            if cal.get("isDefault"):
                return cal.get("guid", "")
        return calendars[0].get("guid", "") if calendars else ""
