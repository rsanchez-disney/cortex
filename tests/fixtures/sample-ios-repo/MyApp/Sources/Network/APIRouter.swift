import Foundation

enum APIRouter {
    case getAccounts
    case createAccount

    var path: String {
        switch self {
        case .getAccounts: return "/v1/accounts"
        case .createAccount: return "/v1/accounts"
        }
    }

    var method: String {
        switch self {
        case .getAccounts: return "GET"
        case .createAccount: return "POST"
        }
    }
}
