module atlas

go 1.26.4

replace golang.org/x/crypto => ./sshpiper.crypto

require (
	github.com/tg123/sshpiper v1.5.4
	github.com/urfave/cli/v2 v2.27.7
)

require (
	github.com/cpuguy83/go-md2man/v2 v2.0.7 // indirect
	github.com/patrickmn/go-cache v2.1.0+incompatible // indirect
	github.com/russross/blackfriday/v2 v2.1.0 // indirect
	github.com/sirupsen/logrus v1.9.4 // indirect
	github.com/tg123/remotesigner v0.0.3 // indirect
	github.com/xrash/smetrics v0.0.0-20250705151800-55b8f293f342 // indirect
	golang.org/x/crypto v0.53.0 // indirect
	golang.org/x/net v0.56.0 // indirect
	golang.org/x/sys v0.46.0 // indirect
	golang.org/x/text v0.38.0 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20260622175928-b703f567277d // indirect
	google.golang.org/grpc v1.81.1 // indirect
	google.golang.org/protobuf v1.36.12-0.20260120151049-f2248ac996af // indirect
)
