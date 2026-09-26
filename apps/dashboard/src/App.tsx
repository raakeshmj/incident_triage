import {
  Content,
  Header,
  HeaderContainer,
  HeaderMenuButton,
  HeaderMenuItem,
  HeaderName,
  HeaderNavigation,
  HeaderSideNavItems,
  SideNav,
  SideNavItems,
  SkipToContent,
  Theme,
} from "@carbon/react";
import { Link, Route, Routes, useLocation } from "react-router-dom";

import { IncidentDetailPage } from "./pages/IncidentDetail";
import { IncidentListPage } from "./pages/IncidentList";
import { OperationsPage } from "./pages/Operations";

const NAV = [
  { to: "/", label: "Incidents", match: (p: string) => !p.startsWith("/operations") },
  { to: "/operations", label: "Operations", match: (p: string) => p.startsWith("/operations") },
];

export function App() {
  const { pathname } = useLocation();
  return (
    <>
      <Theme theme="g100">
        <HeaderContainer
          render={({ isSideNavExpanded, onClickSideNavExpand }) => (
            <Header aria-label="Incident Intelligence">
              <SkipToContent />
              <HeaderMenuButton aria-label={isSideNavExpanded ? "Close menu" : "Open menu"} onClick={onClickSideNavExpand} isActive={isSideNavExpanded} aria-expanded={isSideNavExpanded} />
              <HeaderName as={Link} to="/" prefix="">
                Incident Intelligence
              </HeaderName>
              <HeaderNavigation aria-label="Primary">
                {NAV.map((item) => (
                  <HeaderMenuItem key={item.to} as={Link} to={item.to} isActive={item.match(pathname)} aria-current={item.match(pathname) ? "page" : undefined}>
                    {item.label}
                  </HeaderMenuItem>
                ))}
              </HeaderNavigation>
              <SideNav aria-label="Primary" expanded={isSideNavExpanded} isPersistent={false} onSideNavBlur={onClickSideNavExpand}>
                <SideNavItems>
                  <HeaderSideNavItems>
                    {NAV.map((item) => (
                      <HeaderMenuItem key={item.to} as={Link} to={item.to} isActive={item.match(pathname)} onClick={onClickSideNavExpand}>
                        {item.label}
                      </HeaderMenuItem>
                    ))}
                  </HeaderSideNavItems>
                </SideNavItems>
              </SideNav>
            </Header>
          )}
        />
      </Theme>
      <Content id="main-content" className="ii-content">
        <Routes>
          <Route path="/" element={<IncidentListPage />} />
          <Route path="/incidents/:id" element={<IncidentDetailPage />} />
          <Route path="/operations" element={<OperationsPage />} />
          <Route
            path="*"
            element={
              <div className="ii-page">
                <p>
                  Not found. <Link to="/">Back to incidents</Link>
                </p>
              </div>
            }
          />
        </Routes>
      </Content>
    </>
  );
}
