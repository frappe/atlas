Create an abstraction layer for server providers. For now, we work with Self-Managed and the DigitalOcean provider. But there aren't any nice abstractions. Everything is make-shift.

Here's the rough idea to start.

Provider: An interface to multiple providers. This should translate requests and responses between Atlas and the Provider API.
Provider Settings: Each Provider will have their own Settings DocType. We'll be running one instance of Atlas per physical region. Go with Single DocType.
Provider Implementation: Each provider will implement the public interface.
Provider API: DigitalOcean API / Scaleway / AWS API / Frappe Compute API (To be built) / Human Intervention. Interact with the API with requests

Based on DigitalOcean and Human Intervention API. These are the things you need to create the machine
- Region - slug
- Image - slug
- Machine Size - slug
- Title - Description of the machine (Shown as a title in the provider's interface. Used as hostname)
- Installation related
	- SSH Key - ID/public-key
	- Partition Scheme: Multi-line data/JSON
	- Cloud Init - Multi-line data/SON
- Networking
	- IPv4 / IPv4 / Both
- Tags (varies from vendor to vendor)

Notes:
- Vendors return a unique identifier for every Server we provision. Along with some additional information.
- IDs/slugs are vendor-specific

Networking
- DigitalOcean provides a static /124 IPv6 pool for a droplet
- Most vendors let you reassign /64 pool between servers
- AWS lets you assign /80 to each server from /56 block
- Some let you assign IPs from a /32 block across servers

Providers should implement the following methods
- authenticate: Discover available permissions. Test connection. Fail early if we don't have enough
- discover: Discover available options (images, sizes), capacity, etc.
- provision: Create servers given the above specification
- describe: Get details of a given server
- destroy: Destroy given servers 


Refer to Vendor APIs for getting an idea about the request/response payloads.

DigitalOcean API
- https://docs.digitalocean.com/reference/api/reference/droplets/
Scaleway Metal API
- https://www.scaleway.com/en/developers/api/elastic-metal/servers#create-an-elastic-metal-server
- https://www.scaleway.com/en/developers/api/elastic-metal-flexible-ip
- https://www.scaleway.com/en/developers/api/elastic-metal/private-network
AWS Bare Metal API
- https://docs.aws.amazon.com/boto3/latest/reference/services/ec2/client/run_instances.html

Draft the interface. Keep it as clean as possible.
  
Answer the following questions
- What issues / problems / open questions do you see in this?
- How do we store and translate server-related information? IP Block, Size/Capacity, Image, Region, Keys etc.
- How do we keep track of Provider-specific IDs for these resources?
- How do we restructure the Current Server and Server Provider DocType for handling this?