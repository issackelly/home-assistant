"""
homeassistant.components.recorder
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Component that records all events and state changes.
Allows other components to query this database.
"""
import logging
import threading
import queue
import sqlite3
from datetime import datetime
import time
import json
import atexit

from homeassistant import Event, EventOrigin, State
from homeassistant.remote import JSONEncoder
from homeassistant.const import (
    MATCH_ALL, EVENT_TIME_CHANGED, EVENT_STATE_CHANGED,
    EVENT_HOMEASSISTANT_START, EVENT_HOMEASSISTANT_STOP)

DOMAIN = "recorder"
DEPENDENCIES = []

DB_FILE = 'home-assistant.db'

RETURN_ROWCOUNT = "rowcount"
RETURN_LASTROWID = "lastrowid"
RETURN_ONE_ROW = "one_row"

_INSTANCE = None
_LOGGER = logging.getLogger(__name__)


def query(sql_query, arguments=None):
    """ Query the database. """
    _verify_instance()

    return _INSTANCE.query(sql_query, arguments)


def query_states(state_query, arguments=None):
    """ Query the database and return a list of states. """
    return (
        row for row in
        (row_to_state(row) for row in query(state_query, arguments))
        if row is not None)


def query_events(event_query, arguments=None):
    """ Query the database and return a list of states. """
    return (
        row for row in
        (row_to_event(row) for row in query(event_query, arguments))
        if row is not None)


def row_to_state(row):
    """ Convert a databsae row to a state. """
    try:
        return State(
            row[1], row[2], json.loads(row[3]), datetime.fromtimestamp(row[4]))
    except ValueError:
        # When json.loads fails
        _LOGGER.exception("Error converting row to state: %s", row)
        return None


def row_to_event(row):
    """ Convert a databse row to an event. """
    try:
        return Event(row[1], json.loads(row[2]), EventOrigin[row[3].lower()])
    except ValueError:
        # When json.oads fails
        _LOGGER.exception("Error converting row to event: %s", row)
        return None


def limit_to_run(point_in_time=None):
    """
    Returns a WHERE partial that will limit query to a run.
    A run starts when Home Assistant starts and ends when it stops.
    """
    _verify_instance()

    end_event = None

    # Targetting current run
    if point_in_time is None:
        return "created >= {}".format(
            _adapt_datetime(_INSTANCE.recording_start))

    start_event = query(
        ("SELECT * FROM events WHERE event_type = ? AND created < ? "
         "ORDER BY created DESC LIMIT 0, 1"),
        (EVENT_HOMEASSISTANT_START, point_in_time))[0]

    end_query = query(
        ("SELECT * FROM events WHERE event_type = ? AND created > ? "
         "ORDER BY created ASC LIMIT 0, 1"),
        (EVENT_HOMEASSISTANT_START, point_in_time))

    if end_query:
        end_event = end_query[0]

    where_part = "created >= {}".format(start_event['created'])

    if end_event is None:
        return where_part
    else:
        return "{} and created < {}".format(where_part, end_event['created'])


def setup(hass, config):
    """ Setup the recorder. """
    # pylint: disable=global-statement
    global _INSTANCE

    _INSTANCE = Recorder(hass)

    return True


class Recorder(threading.Thread):
    """
    Threaded recorder
    """
    def __init__(self, hass):
        threading.Thread.__init__(self)

        self.hass = hass
        self.conn = None
        self.queue = queue.Queue()
        self.quit_object = object()
        self.lock = threading.Lock()
        self.recording_start = datetime.now()

        def start_recording(event):
            """ Start recording. """
            self.start()

        hass.bus.listen_once(EVENT_HOMEASSISTANT_START, start_recording)
        hass.bus.listen_once(EVENT_HOMEASSISTANT_STOP, self.shutdown)
        hass.bus.listen(MATCH_ALL, self.event_listener)

    def run(self):
        """ Start processing events to save. """
        self._setup_connection()
        self._setup_run()

        while True:
            event = self.queue.get()

            if event == self.quit_object:
                self._close_run()
                self._close_connection()
                return

            elif event.event_type == EVENT_TIME_CHANGED:
                continue

            elif event.event_type == EVENT_STATE_CHANGED:
                self.record_state(
                    event.data['entity_id'], event.data.get('new_state'))

            self.record_event(event)

    def event_listener(self, event):
        """ Listens for new events on the EventBus and puts them
            in the process queue. """
        self.queue.put(event)

    def shutdown(self, event):
        """ Tells the recorder to shut down. """
        self.queue.put(self.quit_object)

    def record_state(self, entity_id, state):
        """ Save a state to the database. """
        now = datetime.now()

        if state is None:
            info = (entity_id, '', "{}", now, now, now)
        else:
            info = (
                entity_id, state.state, json.dumps(state.attributes),
                state.last_changed, state.last_updated, now)

        self.query(
            "insert into states ("
            "entity_id, state, attributes, last_changed, last_updated,"
            "created) values (?, ?, ?, ?, ?, ?)", info)

    def record_event(self, event):
        """ Save an event to the database. """
        info = (
            event.event_type, json.dumps(event.data, cls=JSONEncoder),
            str(event.origin), datetime.now()
        )

        self.query(
            "insert into events ("
            "event_type, event_data, origin, created"
            ") values (?, ?, ?, ?)", info)

    def query(self, sql_query, data=None, return_value=None):
        """ Query the database. """
        try:
            with self.conn, self.lock:
                _LOGGER.info("Running query %s", sql_query)

                cur = self.conn.cursor()

                if data is not None:
                    cur.execute(sql_query, data)
                else:
                    cur.execute(sql_query)

                if return_value == RETURN_ROWCOUNT:
                    return cur.rowcount
                elif return_value == RETURN_LASTROWID:
                    return cur.lastrowid
                elif return_value == RETURN_ONE_ROW:
                    return cur.fetchone()
                else:
                    return cur.fetchall()

        except sqlite3.IntegrityError:
            _LOGGER.exception(
                "Error querying the database using: %s", sql_query)
            return []

    def _setup_connection(self):
        """ Ensure database is ready to fly. """
        db_path = self.hass.get_config_path(DB_FILE)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

        # Make sure the database is closed whenever Python exits
        # without the STOP event being fired.
        atexit.register(self._close_connection)

        # Have datetime objects be saved as integers
        sqlite3.register_adapter(datetime, _adapt_datetime)

        # Validate we are on the correct schema or that we have to migrate
        cur = self.conn.cursor()

        def save_migration(migration_id):
            """ Save and commit a migration to the database. """
            cur.execute('INSERT INTO schema_version VALUES (?, ?)',
                        (migration_id, datetime.now()))
            self.conn.commit()
            _LOGGER.info("Database migrated to version %d", migration_id)

        try:
            cur.execute('SELECT max(migration_id) FROM schema_version;')
            migration_id = cur.fetchone()[0] or 0

        except sqlite3.OperationalError:
            # The table does not exist
            cur.execute('CREATE TABLE schema_version ('
                        'migration_id integer primary key, performed integer)')
            migration_id = 0

        if migration_id < 1:
            cur.execute("""
                CREATE TABLE recorder_runs (
                    run_id integer primary key,
                    start integer,
                    end integer,
                    closed_incorrect integer default 0,
                    created integer)
            """)

            cur.execute("""
                CREATE TABLE events (
                    event_id integer primary key,
                    event_type text,
                    event_data text,
                    origin text,
                    created integer)
            """)
            cur.execute(
                'CREATE INDEX events__event_type ON events(event_type)')

            cur.execute("""
                CREATE TABLE states (
                    state_id integer primary key,
                    entity_id text,
                    state text,
                    attributes text,
                    last_changed integer,
                    last_updated integer,
                    created integer)
            """)
            cur.execute('CREATE INDEX states__entity_id ON states(entity_id)')

            save_migration(1)

    def _close_connection(self):
        """ Close connection to the database. """
        _LOGGER.info("Closing database")
        atexit.unregister(self._close_connection)
        self.conn.close()

    def _setup_run(self):
        """ Log the start of the current run. """
        if self.query("""UPDATE recorder_runs SET end=?, closed_incorrect=1
                      WHERE end IS NULL""", (self.recording_start, ),
                      return_value=RETURN_ROWCOUNT):

            _LOGGER.warning("Found unfinished sessions")

        self.query(
            "INSERT INTO recorder_runs (start, created) VALUES (?, ?)",
            (self.recording_start, datetime.now()))

    def _close_run(self):
        """ Save end time for current run. """
        self.query(
            "UPDATE recorder_runs SET end=? WHERE start=?",
            (datetime.now(), self.recording_start))


def _adapt_datetime(datetimestamp):
    """ Turn a datetime into an integer for in the DB. """
    return time.mktime(datetimestamp.timetuple())


def _verify_instance():
    """ throws error if recorder not initialized. """
    if _INSTANCE is None:
        raise RuntimeError("Recorder not initialized.")