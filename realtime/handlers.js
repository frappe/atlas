// Bridge browser console sessions to Metal on the node socketio backend. Frappe's
// socketio server loads this file for a site that has Atlas installed. The python
// backend stays the preferred one and uses atlas/realtime/handlers.py instead.
const { get_redis_subscriber } = require("../../frappe/node_utils");

const CONSOLE_TOKEN_PREFIX = "atlas:console:token:";
const MAXIMUM_CONSOLE_INPUT_BYTES = 64 * 1024;
const INVALID_TOKEN_MESSAGE = "This console link is invalid or expired.";
const UNREACHABLE_MESSAGE = "Could not reach the virtual machine console.";
const INVALID_INPUT_MESSAGE = "Console input is invalid.";
const METAL_OPEN_TIMEOUT_MILLISECONDS = 10 * 1000;

// Active bridges by socket ID, and the sockets that are still opening one.
const sessions = new Map();
const opening_sockets = new Set();

let cache_connection = null;

function cache() {
	if (cache_connection) {
		return cache_connection;
	}

	const client = get_redis_subscriber("redis_cache");
	// node-redis raises an uncaught exception, which ends the socketio process, when
	// nothing listens for its errors.
	client.on("error", (error) => console.error("Console cache client failed", error));
	client.on("end", () => {
		cache_connection = null;
	});
	cache_connection = client
		.connect()
		.then(() => client)
		.catch((error) => {
			cache_connection = null;
			throw error;
		});
	return cache_connection;
}

/** Bridge one browser to one Metal console. */
class ConsoleSession {
	constructor(socket, connection) {
		this.socket = socket;
		this.connection = connection;
		this.is_closed = false;

		connection.binaryType = "arraybuffer";
		connection.onmessage = (event) => this.forward_output(event.data);
		connection.onclose = () => this.close();
		connection.onerror = () => this.close();
	}

	forward_output(data) {
		this.socket.emit("atlas_console_output", Buffer.from(data).toString("base64"));
	}

	send_input(data) {
		this.connection.send(data);
	}

	send_resize(cols, rows) {
		this.connection.send(JSON.stringify({ resize: { cols, rows } }));
	}

	/** Close this session once and remove it from the active session map. */
	close() {
		if (this.is_closed) {
			return;
		}
		this.is_closed = true;
		if (sessions.get(this.socket.id) === this) {
			sessions.delete(this.socket.id);
		}
		this.connection.close();
		this.socket.emit("atlas_console_closed");
	}
}

/** Return whether a value has the generated console token format. */
function is_valid_console_token(value) {
	return typeof value === "string" && value.length === 48 && /^[a-z0-9]+$/i.test(value);
}

/** Return the site of one socket. The authenticate middleware checked the namespace. */
function site_of(socket) {
	return socket.nsp.name.slice(1);
}

/** Decode and validate one stored console connection. */
function parse_connection(serialized_connection) {
	const connection = JSON.parse(serialized_connection);
	const url = connection && connection.url;
	const authorization = connection && connection.authorization;
	if (typeof url !== "string" || !is_websocket_url(url)) {
		throw new Error("Console connection has an invalid WebSocket URL");
	}
	if (typeof authorization !== "string" || !authorization || /[\r\n]/.test(authorization)) {
		throw new Error("Console connection has no authorization value");
	}
	return { url, authorization };
}

function is_websocket_url(value) {
	try {
		const parsed = new URL(value);
		return (parsed.protocol === "ws:" || parsed.protocol === "wss:") && Boolean(parsed.host);
	} catch (error) {
		return false;
	}
}

/** Resolve once Metal accepts, refuses, or stops answering the console connection. */
function wait_for_open(connection) {
	return new Promise((resolve) => {
		const timer = setTimeout(() => resolve(false), METAL_OPEN_TIMEOUT_MILLISECONDS);
		const settle = (is_open) => {
			clearTimeout(timer);
			resolve(is_open);
		};
		connection.onopen = () => settle(true);
		connection.onerror = () => settle(false);
		connection.onclose = () => settle(false);
	});
}

/** Consume the token and open the console. */
async function open_console(socket, token) {
	if (sessions.has(socket.id) || opening_sockets.has(socket.id)) {
		return;
	}
	if (!is_valid_console_token(token)) {
		socket.emit("atlas_console_error", INVALID_TOKEN_MESSAGE);
		return;
	}

	opening_sockets.add(socket.id);
	try {
		const client = await cache();
		const serialized_connection = await client.getDel(
			CONSOLE_TOKEN_PREFIX + site_of(socket) + ":" + token
		);
		if (!serialized_connection) {
			socket.emit("atlas_console_error", INVALID_TOKEN_MESSAGE);
			return;
		}

		let connection;
		try {
			connection = parse_connection(serialized_connection);
		} catch (error) {
			console.error(`Invalid console token payload for site ${site_of(socket)}`);
			socket.emit("atlas_console_error", INVALID_TOKEN_MESSAGE);
			return;
		}

		const metal_connection = new WebSocket(connection.url, {
			headers: { Authorization: connection.authorization },
		});
		if (!(await wait_for_open(metal_connection))) {
			metal_connection.close();
			socket.emit("atlas_console_error", UNREACHABLE_MESSAGE);
			return;
		}
		if (!socket.connected) {
			metal_connection.close();
			return;
		}

		sessions.set(socket.id, new ConsoleSession(socket, metal_connection));
		socket.emit("atlas_console_ready");
	} catch (error) {
		console.error(`Console open failed for socket ${socket.id}`, error);
		socket.emit("atlas_console_error", UNREACHABLE_MESSAGE);
	} finally {
		opening_sockets.delete(socket.id);
	}
}

/** Forward viewer keystrokes to the console session. */
function send_console_input(socket, data) {
	const session = sessions.get(socket.id);
	if (!session) {
		return;
	}
	if (typeof data !== "string") {
		socket.emit("atlas_console_error", INVALID_INPUT_MESSAGE);
		return;
	}

	const decoded_data = Buffer.from(data, "base64");
	if (decoded_data.toString("base64") !== data) {
		socket.emit("atlas_console_error", INVALID_INPUT_MESSAGE);
		return;
	}
	if (decoded_data.length > MAXIMUM_CONSOLE_INPUT_BYTES) {
		socket.emit("atlas_console_error", "Console input is too large.");
		return;
	}

	session.send_input(decoded_data);
}

/** Forward a terminal resize to the console session. */
function send_console_resize(socket, size) {
	const session = sessions.get(socket.id);
	if (session && size && typeof size === "object") {
		session.send_resize(terminal_dimension(size.cols, 80), terminal_dimension(size.rows, 24));
	}
}

/** Clamp a viewer terminal size to a sane range. */
function terminal_dimension(value, fallback) {
	if (!Number.isInteger(value)) {
		return fallback;
	}
	return Math.max(1, Math.min(value, 1000));
}

function atlas_handlers(socket) {
	socket.on("atlas_console_open", (token) => open_console(socket, token));
	socket.on("atlas_console_input", (data) => send_console_input(socket, data));
	socket.on("atlas_console_resize", (size) => send_console_resize(socket, size));
	socket.on("disconnect", () => {
		const session = sessions.get(socket.id);
		if (session) {
			session.close();
		}
	});
}

module.exports = atlas_handlers;
