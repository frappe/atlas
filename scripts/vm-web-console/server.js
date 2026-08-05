const express = require("express");
const http = require("http");
const WebSocket = require("ws");
const axios = require("axios");
const fs = require("fs");
const path = require("path");

const app = express();
const server = http.createServer(app);

const wss = new WebSocket.Server({
	server,
});

const ATLAS_BASE_URL =
	process.env.ATLAS_BASE_URL ||
	"http://atlas.localhost:8000";

const ATLAS_VALIDATE_URL =
	`${ATLAS_BASE_URL}/api/method/atlas.atlas.doctype.vm_web_console_api_keys.vm_web_console_api_keys.get_console_session`;

const VM_BASE = "/var/lib/atlas/virtual-machines";

async function validateToken(token) {
	const response = await axios.get(ATLAS_VALIDATE_URL, {
		params: {
			name: token,
		},
	});

	if (!response.data.message) {
		throw new Error("Invalid console token");
	}

	return response.data.message;
}

function openVMConsole(vmUUID) {
	const vmPath = path.join(VM_BASE, vmUUID);

	const fifoIn = path.join(vmPath, "fifo.in");

	const fifoOut = path.join(vmPath, "fifo.out");

	const input = fs.createWriteStream(fifoIn);

	const output = fs.createReadStream(fifoOut);

	return {
		input,
		output,
	};
}

wss.on("connection", (ws) => {
	let consoleSession = null;
	let authenticated = false;
	let vmUUID = null;

	ws.on("message", async (data) => {
		try {
			if (!authenticated) {
				const message = JSON.parse(data.toString());

				if (message.type !== "auth") {
					ws.close(1008, "Authentication required");
					return;
				}

				vmUUID = await validateToken(message.token);

				console.log("Opening console for", vmUUID);

				consoleSession = openVMConsole(vmUUID);

				const { input, output } = consoleSession;

				output.on("data", (chunk) => {
					if (ws.readyState === WebSocket.OPEN) {
						ws.send(chunk);
					}
				});

				output.on("error", (err) => {
					console.error("FIFO output error:", err);
					ws.close(1011, "Console unavailable");
				});

				input.on("error", (err) => {
					console.error("FIFO input error:", err);
					ws.close(1011, "Console unavailable");
				});

				authenticated = true;

				return;
			}


			// Browser -> VM input
			consoleSession.input.write(data);


		} catch (error) {
			console.error(error);
			ws.close(1011, "Console failed");
		}
	});


	ws.on("close", () => {
		console.log("Console closed", vmUUID);

		if (consoleSession) {
			consoleSession.input.destroy();
			consoleSession.output.destroy();
		}
	});
});

app.use(express.static("public"));

server.listen(80, "0.0.0.0", () => {
	console.log("Atlas console listening on port 80");
});
