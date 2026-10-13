# Roll out metald and WG Mesh

Use a Metald Update to install a new metald and Atlas WG Mesh build on the Metal Servers. The two binaries always roll out together. VMs, consoles, and mesh traffic keep running during the upgrade.

## Before you start

- Deploy the Atlas release that contains the build. Atlas builds metald and Atlas WG Mesh during migration and shows their versions in **Atlas Settings**.
- Check that no other Metald Update is **Running** or **Paused**. Only one update can run at a time.

## 1. Create the update

Open **Metal Server**. Select the servers to upgrade (1), open **Actions** (2), and click **Create Metald Update** (3).

![Select servers and create a Metald Update](https://github.com/user-attachments/assets/2f99d3a6-166e-425d-b272-545dd9d5727a)

To upgrade every running server, create a **Metald Update** from its list instead and leave the server table empty.

## 2. Set the batches

The new update is a Draft. It pins the metald and WG Mesh versions from **Atlas Settings**, so a later build does not change it.

Set the rollout fields (1). Check the servers (2), then click **Plan Rollout** (3).

| Field | Use |
| --- | --- |
| Canary Size | Servers in the first batch. Keep it at 1 for a new build. |
| Batch Size | Servers in each later batch. |
| Wait Minutes | Time to watch a batch before Atlas checks it. Use 10 minutes or more in production. |

![Set the rollout fields and plan the rollout](https://github.com/user-attachments/assets/754a5632-c9f6-4116-927a-33448a199023)

## 3. Review the plan

Atlas reads the versions that run on each server (1). A server that already runs the build becomes **Skipped**. A server that Atlas cannot read becomes **Failed**.

Click **Start Rollout** (2). To change the servers or the fields, click **Edit Plan** (3).

![Review the versions and start the rollout](https://github.com/user-attachments/assets/5613cf33-6ab3-47ed-ab75-315af4208eaf)

If all servers already run the build, the update becomes **Completed** without a rollout.

## 4. Watch the canary

The canary server gets batch 1 and the other servers get later batches (1). Atlas starts one upgrade SSH Task for each server and links it in the row (2). **Pause Rollout** and **Cancel Rollout** (3) stay available.

![The canary upgrades first](https://github.com/user-attachments/assets/4c190baa-8e53-4ea8-9a10-3bcdb4461034)

Open the SSH Task to follow the upgrade (1). A successful task (2) shows the new versions and the number of VMs that still run.

![The upgrade SSH Task](https://github.com/user-attachments/assets/73a22631-8a2c-4e4a-8fd3-2e736050aa8b)

## 5. Let Atlas check each batch

After a batch upgrades, Atlas waits until **Wait Until** (1). The row stays **Upgraded** (2) during the wait. Use this time to watch the servers and their VMs.

![Atlas waits before it checks the batch](https://github.com/user-attachments/assets/9deb3c9f-43bd-47bf-92da-508d1089646e)

After the wait, Atlas checks every upgraded server. A server must report the pinned versions and must have synchronized in the last 2 minutes. A server that passes becomes **Verified** (1) with its new versions (2), and the next batch starts.

![A verified canary and the next batch](https://github.com/user-attachments/assets/d2875ca3-d968-4c99-a659-d1bd83a691d3)

## 6. Pause, resume, or cancel

Click **Pause Rollout** to stop new upgrades. The update becomes **Paused** (1). An SSH Task that already runs finishes.

Click **Resume Rollout** (2) to continue. Resume also retries the **Failed** servers. Click **Cancel Rollout** (3) to stop the rollout for good.

![A paused rollout](https://github.com/user-attachments/assets/25b6bf3a-f7ef-4677-97eb-e9fa27445c59)

## 7. Finish

The update becomes **Completed** (1) when every batch is **Verified** (2).

![A completed rollout](https://github.com/user-attachments/assets/60f69852-5428-4780-b93f-bf7464d1e189)

## When a server fails

A failed upgrade or check pauses the update, and the server row becomes **Failed**.

1. Read the row's **Status Reason**. For a failed upgrade, read the end of its SSH Task output.
2. Fix the cause, then click **Resume Rollout**, or click **Cancel Rollout**.

The upgrade script restores the previous binaries when the new build does not start, so a failed server keeps running its old build. A build with a new metald patch is an exception: the server keeps the new build, and you roll forward with a fixed build. [A Metald Update pauses](metal.md#a-metald-update-pauses) lists the host checks.

To go back to an earlier build, duplicate its Metald Update, then plan and start the duplicate. This does not work for a build with a new [metald patch](../develop/metal.md#change-stored-state-with-a-patch) or [WG Mesh map layout](../networking/wg-mesh/operations.md#change-a-map). Roll forward with a fixed build instead.
