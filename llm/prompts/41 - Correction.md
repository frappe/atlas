A few corrections.

Drop the region field from Server. Atlas is single region. Provider might have multi-region API. In that case we set region in the provider settings

We need to store the SSH Key (fingerprint, public key, private key path etc) in a vendor agnostic model. We can add these fields in "Atlas Settings" single DocType. We can use this for all single values. e.g. region, provider.

Single is type of DocType in Frappe. Basically only one row in the table. We're not introducting another DocType.

Only keep truly vendor specific things (what we'll communicate through the API) in the provider settings. Keep the rest in Server / Atlas Settings depending on the case. e.g. ssh_private_key_path should be in Atlas Settings.

Atlas Settings can do the indirection. 

get_provider().provision() -> atlas.provider.provision() 

Server.archive() should call provider.destroy
Server fields should get populated based on the output of describe() in provision_server provider.provision might not give all the values (Unsure)

Scaleway does paritioning based on parition scheme in provision. Maybe this doesn't need to be part of provision. Provider can internally decide.

size and image can be moved to their own DocTypes. Provider Size? Provider Image. The rows can be populated at setup based on vendor, or updated peridically with discover(). This was we can validate the fields Link fields are better validated instead of Data. Unavailable slugs can be marked with enabled=False.

Add a provider_metadata Code field (read-only), to Provider Size, Provider Image, and Server for storing all other fields that the vendor returns.