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

wss.on("connection", async (ws, request) => {
	let consoleSession;

	try {
		const url = new URL(request.url, "http://localhost");

		const token = url.searchParams.get("api");

		if (!token) {
			ws.close(1008, "Missing token");
			return;
		}

		const vmUUID = await validateToken(token);

		console.log("Opening console for", vmUUID);

		consoleSession = openVMConsole(vmUUID);

		const { input, output } = consoleSession;

		/*
                VM -> Browser
            */

		output.on("data", (data) => {
			if (ws.readyState === WebSocket.OPEN) {
				ws.send(data);
			}
		});

		output.on("error", (err) => {
			console.error(err);

			if (ws.readyState === WebSocket.OPEN) {
				ws.close(1011, "Console unavailable");
			}
		});

		input.on("error", (err) => {
			console.error(err);

			if (ws.readyState === WebSocket.OPEN) {
				ws.close(1011, "Console unavailable");
			}
		});

		/*
                Browser -> VM
            */

		ws.on("message", (data) => {
			input.write(data);
		});

		ws.on("close", () => {
			console.log("Console closed", vmUUID);

			input.destroy();
			output.destroy();
		});
	} catch (error) {
		console.error(error);

		ws.close(1011, "Console failed");
	}
});

app.use(express.static("public"));

server.listen(3000, "0.0.0.0", () => {
	console.log("Atlas console listening on port 3000");
});
