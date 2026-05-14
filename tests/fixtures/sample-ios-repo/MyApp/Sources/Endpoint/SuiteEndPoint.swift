import Foundation

enum SuiteEndpoint {
    case getSuiteLanding
    case updateSuiteAccess
    case deleteSuiteAdmin
}

extension SuiteEndpoint: EndPointProtocol {
    var relativeURL: String {
        switch self {
        case .getSuiteLanding:
            return "/suite-admin-management/admins/self/packages/exists"
        case .updateSuiteAccess:
            return "/suite-admin-management/admins/\(adminId)/access"
        case .deleteSuiteAdmin:
            return "/suite-admin-management/admins/\(adminId)"
        }
    }
    var method: String {
        switch self {
        case .getSuiteLanding:
            return URLRequestMethod.get.rawValue
        case .updateSuiteAccess:
            return URLRequestMethod.put.rawValue
        case .deleteSuiteAdmin:
            return URLRequestMethod.delete.rawValue
        }
    }
}
