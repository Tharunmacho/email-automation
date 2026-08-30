This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

## Docker

Build and run the production frontend from this directory:

```bash
docker build --build-arg NEXT_PUBLIC_API_BASE=https://email-automation-emailbackend-tfykto-dc98c8-147-79-71-40.sslip.io -t adira-master-crm-frontend .
docker run --rm -p 3000:3000 adira-master-crm-frontend
```

Or, from the repository root, use the opt-in Compose profile:

```bash
docker compose --profile frontend up --build frontend
```

`NEXT_PUBLIC_API_BASE` is compiled into the browser bundle. Set it to the API's
public URL when deploying the frontend and backend on different origins. Set it
to an empty string when a reverse proxy serves both applications on one origin.

You can start editing the page by modifying `app/page.tsx`. The page auto-updates as you edit the file.

This project uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts) to automatically optimize and load [Geist](https://vercel.com/font), a new font family for Vercel.

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.
changes on the my candidate section
