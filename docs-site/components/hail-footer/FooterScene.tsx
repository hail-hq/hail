/** A quiet pixel town. Decorative artwork stays flush with the footer’s lower edge. */
export function FooterScene() {
  return (
    <div className="hail-footer-art" aria-hidden="true">
      <svg className="hail-footer-scene" viewBox="0 0 1240 135" preserveAspectRatio="xMidYMax slice" aria-hidden="true" shapeRendering="crispEdges"><g fill="currentColor" opacity=".2"><path d="M0 126h1240v8H0zM40 90h32v36H40zM72 65h48v61H72zM130 105h32v21h-32zM192 80h48v46h-48zM200 72h32v8h-32zM260 100h40v26h-40zM380 75h48v51h-48zM392 63h24v12h-24zM790 86h48v40h-48zM846 65h40v61h-40zM858 50h16v15h-16zM950 80h48v46h-48zM1010 101h32v25h-32zM1100 68h44v58h-44zM1112 55h20v13h-20zM1170 92h40v34h-40z"></path></g><g fill="currentColor"><path d="M0 131h1240v4H0zM12 118h8v13h-8zM5 110h22v10H5zM150 118h8v13h-8zM144 108h20v12h-20zM328 113h8v18h-8zM318 102h28v14h-28zM900 116h8v15h-8zM892 105h24v14h-24zM1218 111h8v20h-8zM1210 100h24v14h-24z"></path><path d="M520 116h8v-8h8v-8h8v-8h40v8h8v31h-72zM528 116h56v15h-56zM550 119h10v12h-10zM612 97h8V81h8V65h48v16h8v16h8v34h-80zM632 80h12v12h-12zM656 80h12v12h-12zM642 109h18v22h-18zM710 113h8v-8h8v-8h32v8h8v26h-56z"></path></g><g fill="var(--hf-bg)"><path d="M536 116h8v8h-8zM569 116h8v8h-8zM632 80h12v12h-12zM656 80h12v12h-12zM642 109h18v22h-18zM730 114h12v17h-12z"></path></g><g fill="currentColor" opacity=".45"><path d="M270 39h8v-8h32v8h8v8h-48zM920 30h8v-8h24v8h16v8h-48zM1110 31h8v-8h16v8h8v8h-32z"></path></g><g transform="translate(574 8)"><PixelBirdShape /></g><path d="M385 58h100m220-10h92" stroke="currentColor" strokeWidth="2" strokeDasharray="5 7" opacity=".4"></path></svg>
    </div>
  );
}

/** Shared pixel bird. Keep its shapes identical in the footer and onboarding. */
export function PixelBirdShape() {
  return (<g><path d="M0 20h8V8h8V0h24v8h8v32h-8v8H16v-8H8V28H0z" fill="#5284ff"></path><path d="M48 16h16v8H48z" fill="#f6b858"></path><path d="M32 8h8v8h-8z" fill="white"></path><path d="M36 9h4v6h-4z" fill="#111"></path><path d="M16 48h5v8h-5zM33 48h5v8h-5z" fill="currentColor"></path><path d="M6 29h26v16H6z" fill="#fff"></path><path d="m6 29 13 9 13-9" fill="none" stroke="#1665ff" strokeWidth="2"></path></g>);
}
